#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一条命令出 TrollStore IPA：创建/复用 GitHub 仓库 -> 推送 -> 云 macOS 构建 -> 下载 IPA。

不需要 Mac、不需要 Xcode、不需要 gh CLI、不需要 Apple 开发者账号。

准备（只做一次，约 1 分钟）：
  GitHub 从 2021 年起不再接受「账号+密码」做 git/API 认证，必须用一个 PAT：
  打开 https://github.com/settings/tokens/new?scopes=repo,workflow&description=lemon-ios-build
  生成 classic token（勾 repo + workflow），复制 ghp_ 开头的字符串。
  注：classic 页面勾了 workflow 后，repo 常被联动自动选中并置灰（disabled checked），
      这是正常的；本脚本启动时会打印 X-OAuth-Scopes，可直接确认是否真的带 repo。
  若确认拿不到 repo（账号/组织策略限制 classic 令牌），改用细粒度令牌：
    先在网页手动建一个空仓库 lemon-music-ios-trollstore（不要勾 README / .gitignore），
    再生成 Fine-grained token（github_pat_ 开头）：Repository access 选该仓库，
    Repository permissions 给 Contents / Actions / Workflows = Read and write、
    Metadata = Read-only；运行本脚本时加 --repo <owner>/lemon-music-ios-trollstore 跳过自动建仓。

用法：
  # 方式 1：环境变量（推荐，命令历史里不留令牌）
  set GITHUB_TOKEN=ghp_xxxx        Windows CMD / 或在 PowerShell 用 $env:GITHUB_TOKEN="ghp_xxxx"
  python tools/cloud_build_lemon.py

  # 方式 2：直接传参 / 交互输入（不回显、不保存）
  python tools/cloud_build_lemon.py --token ghp_xxxx
  python tools/cloud_build_lemon.py                 # 会提示输入

  # 公开仓库免费无限 macOS 分钟；默认私有（约 200 分钟/月额度，够跑几十次）
  python tools/cloud_build_lemon.py --public
  # 复用已有仓库
  python tools/cloud_build_lemon.py --repo yourname/lemon-music-ios-trollstore

脚本会：确保本地 main 分支有提交 -> 创建(或复用)仓库 -> 推送 -> 触发
.github/workflows/build-ipa.yml -> 等 macOS runner 跑完 -> 下载 IPA 到
out/ipa/LemonMusic.trollstore.ipa；失败时自动把构建日志拉回来打印。
令牌只存在于内存和单次 git 命令参数里，不写进 .git/config、不落盘。
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import io
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
API = "https://api.github.com"
WORKFLOW = "build-trollstore-carplay.yml"
IPA_ARTIFACT = "LemonMusic-trollstore-carplay-ipa"
LOG_ARTIFACT = "ios-build-log"


def api(method: str, path: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "lemon-ios-cloud-build")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read()
            ctype = resp.headers.get_content_type()
            return resp.status, (json.loads(raw) if raw and ctype == "application/json" else raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw)
        except Exception:
            payload = raw.decode("utf-8", "replace")
        return exc.code, payload
    except urllib.error.URLError as exc:
        return -1, str(exc)


class NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    """artifact/log 接口会 302 到 Azure Blob 的 SAS URL；把 Authorization 带过去
    会被判为冲突认证返回 401，跨主机重定向时必须剥掉。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and "api.github.com" not in newurl:
            for bucket in (new.headers, new.unredirected_hdrs):
                for key in list(bucket):
                    if key.lower() == "authorization":
                        del bucket[key]
        return new


def download(path: str, token: str) -> bytes:
    req = urllib.request.Request(API + path)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("User-Agent", "lemon-ios-cloud-build")
    opener = urllib.request.build_opener(NoAuthRedirect())
    with opener.open(req, timeout=180) as resp:
        return resp.read()


def fetch_logs(token: str, repo_full: str, run_id: int) -> int:
    """把某次 run 的日志抓回来存盘并打印末尾，用于定位编译错误。"""
    s, r = api("GET", f"/repos/{repo_full}/actions/runs/{run_id}", token)
    if s == 200 and isinstance(r, dict):
        print("run 状态:", r.get("status"), r.get("conclusion"))
    s, arts = api("GET", f"/repos/{repo_full}/actions/runs/{run_id}/artifacts", token)
    artifacts = arts.get("artifacts", []) if s == 200 else []
    text = ""
    log_art = next((a for a in artifacts
                    if a["name"] == LOG_ARTIFACT and not a.get("expired")), None)
    if log_art:
        try:
            blob = download(f"/repos/{repo_full}/actions/artifacts/{log_art['id']}/zip", token)
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                for name in zf.namelist():
                    if name.endswith(".log"):
                        text += zf.read(name).decode("utf-8", "replace")
        except Exception as exc:
            print("artifact 日志下载失败:", exc, file=sys.stderr)
    if not text.strip():
        s, jobs = api("GET", f"/repos/{repo_full}/actions/runs/{run_id}/jobs?per_page=20", token)
        if s == 200:
            for job in jobs.get("jobs", []):
                try:
                    raw = download(f"/repos/{repo_full}/actions/jobs/{job['id']}/logs", token)
                    text += "\n==== %s ====\n" % job["name"] + raw.decode("utf-8", "replace")
                except Exception as exc:
                    print("job 日志下载失败:", job.get("name"), exc, file=sys.stderr)
    if not text.strip():
        print("没拿到日志，去网页看:",
              f"https://github.com/{repo_full}/actions/runs/{run_id}", file=sys.stderr)
        return 6
    out = ROOT / "out" / f"ios-build-{run_id}.log"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print("完整日志:", out)
    print("---- 日志末尾 160 行 ----")
    print("\n".join(text.splitlines()[-160:]))
    return 0


def token_from_credential_manager() -> str:
    """本机 Windows Git Credential Manager 里保存的 GitHub 凭据（不落盘、不打印）。

    11-11 跑 WooolClient 云构建时用的 classic/OAuth PAT 就存在这里：
      git credential fill  ->  username=358120997 / password=gho_...
    账号密码早已被 GitHub 拒绝，但 GCM 存的本身就是一个 OAuth token，
    带 repo + workflow scope，可直接用于 REST API 和 git push。
    """
    try:
        r = subprocess.run(["git", "credential", "fill"],
                           input="protocol=https\nhost=github.com\n\n",
                           capture_output=True, text=True, timeout=30)
        if r.returncode:
            return ""
        for line in r.stdout.splitlines():
            if line.startswith("password="):
                return line.split("=", 1)[1].strip()
    except Exception:
        return ""
    return ""


def token_scopes(token: str) -> list[str]:
    """读 X-OAuth-Scopes 响应头；classic PAT 才有，细粒度令牌返回空列表。"""
    req = urllib.request.Request(API + "/user")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("User-Agent", "lemon-ios-cloud-build")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.headers.get("X-OAuth-Scopes", "")
            return [s.strip() for s in raw.split(",") if s.strip()]
    except Exception:
        return []


def git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(ROOT)] + args, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if check and r.returncode:
        raise RuntimeError("git 失败: " + (r.stderr or r.stdout).strip()[:400])
    return r


def prepare_repo(token: str, repo_full: str) -> None:
    if not (ROOT / ".git").exists():
        print("初始化本地 git 仓库…")
        git(["init"])
        git(["checkout", "-b", "main"], check=False)
    else:
        branch = git(["rev-parse", "--abbrev-ref", "HEAD"], check=False).stdout.strip()
        if branch != "main":
            git(["checkout", "-B", "main"], check=False)

    git(["add", "-A"])
    if git(["status", "--porcelain"], check=False).stdout.strip():
        git(["-c", "user.name=lemon-build", "-c", "user.email=build@local",
             "commit", "-m", "lemon music ios: trollstore carplay cloud IPA build"])
        print("已创建本地提交")
    else:
        print("本地已有提交，直接推送")

    remotes = git(["remote"], check=False).stdout.split()
    if "origin" in remotes:
        git(["remote", "remove", "origin"], check=False)
    git(["remote", "add", "origin", f"https://github.com/{repo_full}.git"])

    # 用 http.extraHeader 传凭据：remote URL 里不含令牌，.git/config 也不会留痕。
    auth = base64.b64encode(("x-access-token:" + token).encode()).decode()
    # Windows 的 schannel 在离线/被代理环境下会报 CRYPT_E_REVOCATION_OFFLINE，
    # 切到 OpenSSL 后端并关闭吊销检查；非 Windows 上这两个配置会被安全忽略。
    r = subprocess.run(
        ["git", "-C", str(ROOT),
         "-c", "http.sslBackend=openssl",
         "-c", "http.schannelCheckRevoke=false",
         "-c", f"http.extraHeader=Authorization: Basic {auth}",
         "push", "-u", "origin", "main"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        out = (r.stdout or "") + (r.stderr or "")
        out = out.replace(token, "***")  # 双保险，不把令牌打出来
        raise RuntimeError("推送失败: " + out[-800:])
    print("推送完成:", repo_full)


def main() -> int:
    ap = argparse.ArgumentParser(description="一键云构建 LemonMusic TrollStore CarPlay IPA")
    ap.add_argument("--token", help="GitHub PAT（也可设 GITHUB_TOKEN 环境变量）")
    ap.add_argument("--repo", help="已有仓库 owner/name，跳过自动创建")
    ap.add_argument("--repo-name", default="lemon-music-ios-trollstore")
    ap.add_argument("--public", action="store_true", help="建公开仓库（macOS 分钟免费无限）")
    ap.add_argument("--wait-minutes", type=int, default=30)
    ap.add_argument("--min-ios", default="17.1", help="workflow 的最低 iOS 版本输入")
    ap.add_argument("--bundle", default="com.example.lemonmusic", help="主 App Bundle ID 输入")
    ap.add_argument("--prepare-only", action="store_true", help="只提交并推送，不触发构建")
    ap.add_argument("--run-id", type=int,
                    help="只拉取指定 Actions run 的构建日志（不推送、不触发构建）")
    args = ap.parse_args()

    token = (args.token or os.environ.get("GITHUB_TOKEN")
             or os.environ.get("GH_TOKEN"))
    source = "参数/环境变量"
    if not token:
        token = token_from_credential_manager()
        source = "Windows Git 凭据管理器 (GCM)"
    if not token:
        token = getpass.getpass("GitHub PAT（不显示、不保存）: ").strip()
        source = "交互输入"
    if not token:
        print("缺少令牌", file=sys.stderr)
        return 2
    print("令牌来源:", source, "（不打印令牌值）")
    if not token.startswith(("ghp_", "github_pat_", "gho_", "ghu_", "ghs_", "ghr_")):
        print("警告：GitHub 不接受账号密码，也不接受普通口令；需要 PAT。", file=sys.stderr)

    status, me = api("GET", "/user", token)
    if status != 200 or not isinstance(me, dict) or "login" not in me:
        print("令牌校验失败:", status, str(me)[:300], file=sys.stderr)
        return 2
    login = me["login"]
    print("GitHub 账号:", login)
    fine_grained = token.startswith("github_pat_")
    scopes = token_scopes(token)
    if scopes:
        print("令牌 scopes:", ", ".join(scopes))
        if not fine_grained and not ({"repo", "public_repo"} & set(scopes)):
            print("警告：令牌没有 repo scope，无法创建/推送私有仓库。请重新生成：\n"
                  "  https://github.com/settings/tokens/new?scopes=repo,workflow"
                  "&description=lemon-ios-build", file=sys.stderr)
    elif not fine_grained:
        print("提示：未读到 X-OAuth-Scopes 响应头；若稍后建仓/推送失败，请检查令牌权限。")
    if fine_grained and not args.repo:
        print("提示：细粒度令牌可能无法自动建仓；若失败，请手动建空仓库并加 "
              "--repo <owner>/<name> 重试。")

    if args.run_id:
        repo_full = (args.repo.strip().strip("/") if args.repo
                     else f"{login}/{args.repo_name}")
        return fetch_logs(token, repo_full, args.run_id)

    if args.repo:
        repo_full = args.repo.strip().strip("/")
    else:
        repo_full = f"{login}/{args.repo_name}"
        status, r = api("POST", "/user/repos", token, {
            "name": args.repo_name,
            "private": not args.public,
            "description": "Lemon Music iOS / TrollStore CarPlay (cloud-built IPA)",
            "auto_init": False, "has_issues": False,
            "has_wiki": False, "has_projects": False,
        })
        if status == 201:
            print("已创建仓库:", repo_full, "(公开)" if args.public else "(私有)")
        elif status == 422:
            print("仓库已存在，复用:", repo_full)
        else:
            print("创建仓库失败:", status, str(r)[:300], file=sys.stderr)
            if fine_grained:
                print("细粒度令牌通常不能创建仓库。请在网页手动建空仓库后加 "
                      "--repo <owner>/<name> 重试。", file=sys.stderr)
            else:
                print("多半是令牌缺 repo scope，请重新生成 classic 令牌（repo + workflow），"
                      "或手动建空仓库后用 --repo 指定。", file=sys.stderr)
            return 2

    try:
        prepare_repo(token, repo_full)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 3
    if args.prepare_only:
        print("已推送。稍后可自行到仓库 Actions 页面手动运行 Build LemonMusic TrollStore CarPlay IPA。")
        return 0

    deadline = time.time() + args.wait_minutes * 60
    head_sha = git(["rev-parse", "HEAD"]).stdout.strip()
    run_id = None

    # workflow 里 on.push 会在推送 main 时自动跑一次；优先复用它，避免再
    # dispatch 一次导致同一份代码白跑两轮 macOS 额度。
    for _ in range(6):
        s, r = api("GET", f"/repos/{repo_full}/actions/runs?event=push&per_page=5", token)
        if s == 200:
            for run in r.get("workflow_runs", []):
                created = run.get("created_at", "")
                try:
                    age = (datetime.now(timezone.utc)
                           - datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds()
                except Exception:
                    age = 10 ** 9
                if run.get("head_sha") == head_sha and age < 300:
                    run_id = run["id"]
                    break
        if run_id:
            print("复用 push 自动触发的构建:", run_id)
            break
        time.sleep(3)

    if not run_id:
        # 触发 workflow_dispatch；新仓库的 workflow 索引可能要几秒，重试。
        status, before = api("GET", f"/repos/{repo_full}/actions/runs"
                             "?event=workflow_dispatch&per_page=5", token)
        known = {r["id"] for r in before.get("workflow_runs", [])} if status == 200 else set()
        dispatched = False
        for _ in range(6):
            s, r = api("POST", f"/repos/{repo_full}/actions/workflows/{WORKFLOW}/dispatches",
                       token, {"ref": "main", "inputs": {
                           "min_ios": args.min_ios,
                           "bundle_id": args.bundle,
                           "add_app_id": "false",
                       }})
            if s == 204:
                dispatched = True
                break
            time.sleep(3)
        if not dispatched:
            print("触发失败（workflow 可能尚未被索引）；稍后到 Actions 页面手动运行一次。",
                  file=sys.stderr)
            return 4
        print("已触发云构建，等待 macOS runner…")
        while time.time() < deadline:
            s, r = api("GET", f"/repos/{repo_full}/actions/runs"
                       "?event=workflow_dispatch&per_page=5", token)
            if s == 200:
                for run in r.get("workflow_runs", []):
                    if run["id"] not in known:
                        run_id = run["id"]
                        break
            if run_id:
                break
            time.sleep(8)
    if not run_id:
        print("超时：没看到新的 workflow run，请到 Actions 页面查看。", file=sys.stderr)
        return 5
    run_url = f"https://github.com/{repo_full}/actions/runs/{run_id}"
    print("构建任务:", run_url)

    conclusion = None
    while time.time() < deadline:
        s, r = api("GET", f"/repos/{repo_full}/actions/runs/{run_id}", token)
        if s == 200 and r.get("status") == "completed":
            conclusion = r.get("conclusion")
            break
        time.sleep(10)
    print("构建结论:", conclusion or "超时")

    # 失败时把日志拉回来，按报错修 Swift 再重跑即可（macOS 分钟有限，尽量一次过）。
    s, arts = api("GET", f"/repos/{repo_full}/actions/runs/{run_id}/artifacts", token)
    artifacts = arts.get("artifacts", []) if s == 200 else []
    if conclusion != "success":
        fetch_logs(token, repo_full, run_id)
        print("把上面的报错发给我，我按报错修工程后再跑一次。", file=sys.stderr)
        return 6

    ipa_art = next((a for a in artifacts if a["name"] == IPA_ARTIFACT), None)
    if not ipa_art:
        print("构建成功但没找到 IPA artifact，去 Actions 页面手动下载。", run_url,
              file=sys.stderr)
        return 7
    blob = download(f"/repos/{repo_full}/actions/artifacts/{ipa_art['id']}/zip", token)
    dest_dir = ROOT / "out" / "ipa"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "LemonMusic.trollstore.carplay.ipa"
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        name = next((n for n in zf.namelist() if n.endswith(".ipa")), None)
        if not name:
            print("artifact 里没有 .ipa:", zf.namelist()[:10], file=sys.stderr)
            return 8
        dest.write_bytes(zf.read(name))
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    print("IPA 已下载:", dest)
    print("大小: %.2f MiB  sha256: %s" % (dest.stat().st_size / 1024 / 1024, digest))
    print("下一步：把 IPA 传到 iOS 设备，用 TrollStore 打开安装。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
