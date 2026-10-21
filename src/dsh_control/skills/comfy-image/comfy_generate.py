#!/usr/bin/env python3
"""家里生图客户端（Qwen-Image-2.1-Turbo / ComfyUI 原生接口，纯标准库）。

只吃环境变量，代码里不写任何机器地址——地址与 token 由控制台铺在同目录的
config.env 里（COMFY_URL / COMFY_BACKUP_URL / COMFY_TOKEN）。

    python3 comfy_generate.py --status
    python3 comfy_generate.py "一枚蓝色徽章，米白底" --w 1024 --h 1024 --out 徽章.png
    python3 comfy_generate.py "保持这套配色，标题换成「第二季度」" --ref 卡片1.png --out 卡片2.png

协议是异步三步（提交 → 轮询 → 取文件），没有同步返回：
    POST /prompt → prompt_id；GET /history/{id} 出现即完成；GET /view?filename=… 取 PNG。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UNET = "qwen_image_2.1_turbo_int8_convrot.safetensors"
CLIP = "qwen3vl_8b_w4a8.safetensors"
VAE = "qwen_image_2.1_vae_bf16.safetensors"
TOTAL_TIMEOUT = float(os.environ.get("COMFY_TIMEOUT", "180"))


def _bases() -> list[str]:
    out = []
    for key in ("COMFY_URL", "COMFY_BACKUP_URL"):
        v = (os.environ.get(key) or "").strip().rstrip("/")
        if v and v not in out:
            out.append(v)
    return out


def _token() -> str:
    tok = (os.environ.get("COMFY_TOKEN") or "").strip()
    path = (os.environ.get("COMFY_TOKEN_FILE") or "").strip()
    if not tok and path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            tok = fh.read().strip()
    return tok


class Dead(Exception):
    """带上"下一步该做什么"的失败，别让会话自己推理。"""


def _request(base: str, path: str, *, data=None, timeout: float = 30,
             binary: bool = False):
    req = urllib.request.Request(base + path)
    req.add_header("Authorization", "Bearer " + _token())
    if data is not None:
        req.data = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw if binary else json.loads(raw or b"{}")


def call(path: str, *, data=None, timeout: float = 30, binary: bool = False):
    """按 config.env 里的顺序试；全挂才报错，并说清每一条为什么挂。"""
    bases = _bases()
    if not bases:
        raise Dead("没读到 COMFY_URL：技能目录里的 config.env 缺失或被删了，"
                   "请在控制台点一次「重启」（会重铺生图配置）")
    if not _token():
        raise Dead("没读到 COMFY_TOKEN：告诉管理员「生图 token 需要重新铺一次」")
    errors = []
    for base in bases:
        try:
            return _request(base, path, data=data, timeout=timeout, binary=binary)
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001, S110 - 错误体读不出来不影响判读
                pass
            errors.append(f"{base} → HTTP {exc.code} {body}")
            if exc.code == 401:
                raise Dead("401：token 不对或过期。找管理员重铺一次生图配置，"
                           "不要改这里的代码") from exc
            if exc.code == 502:
                raise Dead("502：门卫活着但生图后端没起来，最常见是语音转写正占着那张卡"
                           "（两个栈互斥）。找管理员把生图栈切回来，"
                           "别反复重试——重交会排两次队") from exc
        except Exception as exc:  # noqa: BLE001 - 连不上要换下一条入口
            errors.append(f"{base} → {type(exc).__name__}: {exc}")
    raise Dead("生图服务连不上（" + "；".join(errors) + "）。"
               "最可能是那台 Windows 机器没开机；不要在这台机器上试图重启它")


def workflow(prompt: str, w: int, h: int, seed: int, *, ref_name: str | None = None,
             denoise: float = 1.0, prefix: str = "dsh") -> dict:
    """固定 8 节点图。只有节点 4 的提示词、节点 5 的尺寸、节点 6 的 seed/denoise 该变。"""
    wf = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": UNET, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": CLIP, "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "4": {"class_type": "TextEncodeQwenImage21",
              "inputs": {"clip": ["2", 0], "prompt": prompt, "negative_prompt": "",
                         "vae": ["3", 0], "resolution": min(w, h)}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": w, "height": h, "batch_size": 1}},
        "6": {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["4", 1],
                         "latent_image": ["5", 0], "seed": seed, "steps": 8, "cfg": 1,
                         "sampler_name": "euler", "scheduler": "linear_quadratic",
                         "denoise": denoise}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
        "8": {"class_type": "SaveImage",
              "inputs": {"images": ["7", 0], "filename_prefix": prefix}},
    }
    if ref_name:  # 图生图：latent 换成参考图编码，denoise 由调用方给
        wf["9"] = {"class_type": "LoadImage", "inputs": {"image": ref_name}}
        wf["10"] = {"class_type": "VAEEncode",
                    "inputs": {"pixels": ["9", 0], "vae": ["3", 0]}}
        wf["6"]["inputs"]["latent_image"] = ["10", 0]
    return wf


def upload(path: str) -> str:
    """multipart 上传参考图，返回 ComfyUI 侧的文件名。"""
    boundary = "----dshfamily"
    with open(path, "rb") as fh:
        body = fh.read()
    parts = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
        f"filename=\"{os.path.basename(path)}\"\r\n"
        "Content-Type: image/png\r\n\r\n").encode() + body + (
        f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"type\"\r\n\r\n"
        "input\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\n"
        f"true\r\n--{boundary}--\r\n").encode()
    base = _bases()[0]
    req = urllib.request.Request(base + "/upload/image", data=parts)
    req.add_header("Authorization", "Bearer " + _token())
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())["name"]


def wait_done(prompt_id: str, started: float) -> dict:
    """完成判据 = history 里出现这个 id。history 随后端重启会清空，所以必须带超时。"""
    while True:
        hist = call(f"/history/{urllib.parse.quote(prompt_id)}")
        if prompt_id in hist:
            return hist[prompt_id]
        if time.time() - started > TOTAL_TIMEOUT:
            raise Dead(f"等了 {TOTAL_TIMEOUT:.0f} 秒还没出图：前面可能有人在批量出图。"
                       "别重复提交，稍后再试一次")
        time.sleep(2)


def images_of(entry: dict) -> list[dict]:
    out = []
    for node in (entry.get("outputs") or {}).values():
        out.extend(node.get("images") or [])
    return out


def download(img: dict, dest: str) -> str:
    q = urllib.parse.urlencode({"filename": img["filename"],
                                "subfolder": img.get("subfolder", ""),
                                "type": img.get("type", "output")})
    blob = call("/view?" + q, timeout=120, binary=True)
    with open(dest, "wb") as fh:
        fh.write(blob)
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description="家里生图（ComfyUI / Qwen-Image-2.1-Turbo）")
    ap.add_argument("prompt", nargs="?", help="提示词；--status 时可省略")
    ap.add_argument("--w", type=int, default=1024)
    ap.add_argument("--h", type=int, default=1024)
    ap.add_argument("--seed", type=int, default=None, help="不传则随机（建议别传）")
    ap.add_argument("--out", default=None, help="存到哪（默认 生图-<seed>.png）")
    ap.add_argument("--ref", default=None, help="参考图路径：图生图，保持系列一致性")
    ap.add_argument("--denoise", type=float, default=0.55, help="图生图改动幅度 0.4–0.6")
    ap.add_argument("--status", action="store_true", help="只看服务与显存")
    args = ap.parse_args()

    try:
        if args.status:
            stats = call("/system_stats", timeout=20)
            sysinfo = stats.get("system") or {}
            # 显存在**顶层** devices 数组里（`system.devices` 是空的），字段名是
            # vram_total / vram_free。拿不到就直说，别用 0 冒充"卡满了/卡空了"。
            dev = (stats.get("devices") or [{}])[0]
            total = int(dev.get("vram_total", 0))
            free = int(dev.get("vram_free", 0))
            queue = call("/queue", timeout=20)
            line = f"生图服务在线（{sysinfo.get('comfyui_version', '版本未知')}）"
            if total:
                line += (f"；显存空闲 {free // 1024 // 1024} / {total // 1024 // 1024} MiB"
                         f"（{dev.get('name', '未知卡')}）")
            else:
                line += "；没读到显存信息"
            line += (f"；队列 running={len(queue.get('queue_running', []))} "
                     f"pending={len(queue.get('queue_pending', []))}")
            if total and free < 6 * 1024 * 1024 * 1024:
                line += ("\n  说明：显存大部分被已加载的模型占着（画完不会主动释放，"
                         "下次要换模型时会自动卸载重载，那一单会慢一些）。这不用处理；"
                         "只有出图真失败了才找管理员，别自己反复重交")
            print(line)
            return 0

        if not args.prompt:
            ap.error("要生图请给提示词，或者用 --status 只看状态")
        for name, val in (("宽", args.w), ("高", args.h)):
            if val % 16:
                ap.error(f"{name} {val} 不是 16 的倍数（会被静默取整，别踩这个坑）")

        seed = args.seed if args.seed is not None else random.randint(1, 2**31 - 1)
        ref_name = upload(args.ref) if args.ref else None
        wf = workflow(args.prompt, args.w, args.h, seed, ref_name=ref_name,
                      denoise=args.denoise if ref_name else 1.0,
                      prefix=f"dsh{seed % 1000:03d}")
        sent = call("/prompt", data={"prompt": wf, "client_id": "dsh-family"}, timeout=60)
        prompt_id = sent.get("prompt_id")
        if not prompt_id:
            raise Dead(f"提交没拿到 prompt_id：{json.dumps(sent, ensure_ascii=False)[:300]}")
        started = time.time()
        print(f"已提交（seed={seed}），等出图……", file=sys.stderr)
        entry = wait_done(prompt_id, started)
        imgs = images_of(entry)
        if not imgs:
            raise Dead("任务结束了但没有产物图片：" +
                       json.dumps(entry.get("status", {}), ensure_ascii=False)[:300])
        dest = args.out or f"生图-{seed}.png"
        for idx, img in enumerate(imgs):
            target = dest if len(imgs) == 1 else f"{idx + 1:02d}-{dest}"
            download(img, target)
            print(f"已存 {target}（{os.path.getsize(target) // 1024} KB，"
                  f"耗时 {time.time() - started:.1f} 秒）——"
                  "交付前请先 read_image 看一眼有没有丢字或假字")
        return 0
    except Dead as dead:
        print(f"生图失败：{dead}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as exc:  # 兜底：把响应体摊出来
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:400]
        except Exception:  # noqa: BLE001, S110 —— 读不出响应体也照样把状态码报出来
            pass
        print(f"生图失败：HTTP {exc.code} {body}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
