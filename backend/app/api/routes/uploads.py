"""头像上传接口：`POST /uploads/avatar`（M9 后半，D29）。

🔴 四道闸门（D29，一道都不能省）：
1. **大小** ≤ 10MB（读出来数字节，不信任 Content-Length）
2. **魔数嗅探**：只收 JPEG / PNG / WebP —— SVG 是 XSS 载体、GIF 有多帧坑，**一律不收**；
   扩展名不可信，只看文件头
3. **真实解码** + 解压炸弹防护：Pillow `verify()` 验完整性，像素总数超上限直接拒
   （一张 40000×40000 的 PNG 解码后能吃掉几个 G 内存）
4. **强制重编码**：中心裁方 → 缩到 512×512 → 存 WebP（quality=85），
   文件名换 UUID —— 原图（可能带恶意元数据/超尺寸）根本不落盘

产物放 `backend/uploads/avatars/`，由 main.py 以 `/uploads` 静态挂载。
返回的 url 是**相对路径**（`/uploads/avatars/xxx.webp`）—— 前端直接用它 +
PATCH /auth/me 存进 profile；同源部署下天然正确，跨域部署也不带域名漂移。
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Request, UploadFile

from app.api.deps import get_current_user_id, get_limiter
from app.api.errors import AppError, RateLimited
from app.api.ratelimit import AVATAR_HOURLY
from app.schemas import AvatarUploadResponse

router = APIRouter(tags=["uploads"])

MAX_BYTES = 10 * 1024 * 1024  # 10MB（D29）
MAX_PIXELS = 40_000_000  # 解压炸弹上限（约 6300×6300）
SIDE = 512  # 重编码目标边长
AVATAR_DIR = Path(__file__).resolve().parents[3] / "uploads" / "avatars"

# 魔数白名单：JPEG(FF D8 FF) / PNG(89 50 4E 47) / WebP(RIFF....WEBP)
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
)


def _sniff_format(data: bytes) -> str | None:
    for magic, fmt in _MAGIC:
        if data.startswith(magic):
            return fmt
    # WebP：RIFF 小端长度(4B) + "WEBP"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


@router.post("/uploads/avatar", response_model=AvatarUploadResponse)
async def upload_avatar(
    request: Request,
    file: UploadFile,
    user_id: int = Depends(get_current_user_id),
) -> AvatarUploadResponse:
    limiter = get_limiter(request)
    retry_after = limiter.check(f"{AVATAR_HOURLY.name}:{user_id}", AVATAR_HOURLY)
    if retry_after:
        raise RateLimited(f"上传太频繁，请 {retry_after} 秒后重试", retry_after)

    # ── 闸门 1：大小（先读再数，超了立刻拒，不再往下走解码）──
    data = await file.read()
    if len(data) > MAX_BYTES:
        raise AppError("file_too_large", "头像不能超过 10MB", 400)
    if not data:
        raise AppError("invalid_file", "文件为空", 400)

    # ── 闸门 2：魔数嗅探（SVG/GIF/伪装扩展名全在这里倒下）──
    fmt = _sniff_format(data)
    if fmt is None:
        raise AppError("unsupported_format", "仅支持 JPG / PNG / WebP 图片", 400)

    # ── 闸门 3：真实解码 + 解压炸弹防护 ──
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data))
        img.load()  # verify() 之后不能再用，直接 load() —— 解不动/截断的文件在这里抛
        if img.width * img.height > MAX_PIXELS:
            raise AppError("file_too_large", "图片尺寸过大", 400)
        if img.format not in ("JPEG", "PNG", "WEBP"):
            raise AppError("unsupported_format", "仅支持 JPG / PNG / WebP 图片", 400)
    except AppError:
        raise
    except Exception as exc:  # PIL.UnidentifiedImageError / OSError / DecompressionBombError
        raise AppError("invalid_file", "图片文件无法解码", 400) from exc

    # ── 闸门 4：中心裁方 → 512×512 → WebP，UUID 文件名落盘 ──
    w, h = img.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((SIDE, SIDE))
    if img.mode != "RGB":
        img = img.convert("RGB")

    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.webp"  # 用户原始文件名不参与 —— 路径注入/XSS 一刀切
    out = AVATAR_DIR / filename
    img.save(out, format="WEBP", quality=85)

    return AvatarUploadResponse(url=f"/uploads/avatars/{filename}")
