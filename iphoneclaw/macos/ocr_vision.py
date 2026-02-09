from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional

import Quartz
from AppKit import (
    NSBezierPath,
    NSBitmapImageRep,
    NSColor,
    NSDeviceRGBColorSpace,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSGraphicsContext,
    NSMakeRect,
    NSPNGFileType,
)
from Foundation import NSString

from iphoneclaw.types import Rect, ScreenshotOutput


def _clamp01(v: float) -> float:
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def _decode_screenshot_to_cgimage(image_b64: str):
    try:
        raw = base64.b64decode(image_b64)
    except Exception as e:
        raise RuntimeError("invalid screenshot base64: %s" % str(e)) from e

    cf_data = Quartz.CFDataCreate(None, raw, len(raw))
    if cf_data is None:
        raise RuntimeError("failed to create CFData from screenshot bytes")

    src = Quartz.CGImageSourceCreateWithData(cf_data, None)
    if src is None:
        raise RuntimeError("failed to decode screenshot bytes as image")

    cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    if cg is None:
        raise RuntimeError("failed to build CGImage from screenshot data")
    return cg


def _rect_from_vision_bbox_top_left(
    bbox_bottom_left: Any,
    *,
    image_width: int,
    image_height: int,
    window_bounds: Rect,
    coord_factor: int,
) -> Dict[str, Any]:
    # Vision bbox is normalized [0,1], origin at bottom-left.
    vx = float(bbox_bottom_left.origin.x)
    vy = float(bbox_bottom_left.origin.y)
    vw = float(bbox_bottom_left.size.width)
    vh = float(bbox_bottom_left.size.height)

    nx = _clamp01(vx)
    ny = _clamp01(1.0 - (vy + vh))
    nw = _clamp01(vw)
    nh = _clamp01(vh)

    px = int(round(nx * float(image_width)))
    py = int(round(ny * float(image_height)))
    pw = int(round(nw * float(image_width)))
    ph = int(round(nh * float(image_height)))

    sx = float(window_bounds.x + nx * window_bounds.width)
    sy = float(window_bounds.y + ny * window_bounds.height)
    sw = float(nw * window_bounds.width)
    sh = float(nh * window_bounds.height)

    mx = int(round(nx * float(coord_factor)))
    my = int(round(ny * float(coord_factor)))
    mw = int(round(nw * float(coord_factor)))
    mh = int(round(nh * float(coord_factor)))

    return {
        "normalized_box": {
            "x": round(nx, 6),
            "y": round(ny, 6),
            "width": round(nw, 6),
            "height": round(nh, 6),
        },
        "pixel_box": {
            "x": px,
            "y": py,
            "width": pw,
            "height": ph,
        },
        "screen_box": {
            "x": round(sx, 3),
            "y": round(sy, 3),
            "width": round(sw, 3),
            "height": round(sh, 3),
        },
        "model_box": {
            "x": mx,
            "y": my,
            "width": mw,
            "height": mh,
        },
    }


def recognize_screenshot_text(
    shot: ScreenshotOutput,
    *,
    coord_factor: int = 1000,
    min_confidence: float = 0.0,
    max_items: Optional[int] = None,
    languages: Optional[List[str]] = None,
    auto_detect_language: bool = True,
) -> Dict[str, Any]:
    try:
        import Vision  # type: ignore
    except Exception as e:
        raise RuntimeError(
            "Apple Vision framework is unavailable. Install pyobjc-framework-Vision on macOS."
        ) from e

    cg = _decode_screenshot_to_cgimage(shot.base64)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    # Accurate mode gives better OCR quality on UI text.
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(False)
    try:
        req.setAutomaticallyDetectsLanguage_(bool(auto_detect_language))
    except Exception:
        # Older macOS/Vision may not expose this setter.
        pass

    # Default language set: Simplified Chinese + Traditional Chinese + English.
    lang_list = [str(x).strip() for x in (languages or []) if str(x).strip()]
    if not lang_list:
        lang_list = ["zh-Hans", "zh-Hant", "en-US"]
    try:
        req.setRecognitionLanguages_(lang_list)
    except Exception as e:
        raise RuntimeError("failed to set OCR languages %r: %s" % (lang_list, str(e))) from e

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError("Vision OCR request failed: %s" % (str(err) if err is not None else "unknown error"))

    obs = req.results() or []
    items: List[Dict[str, Any]] = []
    for o in obs:
        cands = o.topCandidates_(1) or []
        if not cands:
            continue
        best = cands[0]
        text = str(best.string() or "").strip()
        if not text:
            continue
        conf = float(best.confidence())
        if conf < float(min_confidence):
            continue

        rects = _rect_from_vision_bbox_top_left(
            o.boundingBox(),
            image_width=int(shot.image_width),
            image_height=int(shot.image_height),
            window_bounds=shot.window_bounds,
            coord_factor=int(coord_factor),
        )
        items.append(
            {
                "text": text,
                "confidence": round(conf, 4),
                **rects,
            }
        )

    # Reading order: top-to-bottom, then left-to-right.
    items.sort(key=lambda x: (float(x["normalized_box"]["y"]), float(x["normalized_box"]["x"])))
    if max_items is not None and int(max_items) > 0:
        items = items[: int(max_items)]

    return {
        "engine": "apple-vision",
        "recognition_level": "accurate",
        "recognition_languages": lang_list,
        "auto_detect_language": bool(auto_detect_language),
        "coord_factor": int(coord_factor),
        "count": len(items),
        "items": items,
        "screenshot": {
            "scale_factor": float(shot.scale_factor),
            "window_bounds": asdict(shot.window_bounds),
            "image_width": int(shot.image_width),
            "image_height": int(shot.image_height),
            "crop_rect_px": list(shot.crop_rect_px) if shot.crop_rect_px else None,
            "raw_image_width": int(shot.raw_image_width),
            "raw_image_height": int(shot.raw_image_height),
        },
    }


def save_ocr_debug_visualization(
    shot: ScreenshotOutput,
    payload: Dict[str, Any],
    *,
    out_dir: str,
    prefix: str = "ocr",
) -> Dict[str, str]:
    """
    Save OCR debug artifacts:
      1) raw screenshot jpg
      2) overlay png with recognized boxes
      3) text-layer png (white background + OCR text/boxes)
      4) ocr json
    """
    os.makedirs(out_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    stem = "%s_%s_%03d" % (str(prefix or "ocr"), ts, int((time.time() * 1000.0) % 1000.0))

    raw_path = os.path.abspath(os.path.join(out_dir, stem + "_raw.jpg"))
    overlay_path = os.path.abspath(os.path.join(out_dir, stem + "_overlay.png"))
    text_layer_path = os.path.abspath(os.path.join(out_dir, stem + "_text_layer.png"))
    json_path = os.path.abspath(os.path.join(out_dir, stem + "_ocr.json"))

    # Save raw screenshot bytes.
    raw = base64.b64decode(shot.base64)
    with open(raw_path, "wb") as f:
        f.write(raw)

    # Save OCR payload json.
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # Draw overlay boxes on top of screenshot.
    cg = _decode_screenshot_to_cgimage(shot.base64)
    w = int(Quartz.CGImageGetWidth(cg))
    h = int(Quartz.CGImageGetHeight(cg))
    if w <= 0 or h <= 0:
        raise RuntimeError("invalid screenshot size for debug overlay: %dx%d" % (w, h))

    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(
        None,
        w,
        h,
        8,
        0,
        cs,
        Quartz.kCGImageAlphaPremultipliedLast,
    )
    if ctx is None:
        raise RuntimeError("failed to create bitmap context for debug overlay")

    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, w, h), cg)
    Quartz.CGContextSetLineWidth(ctx, 2.0)

    items = payload.get("items") or []
    for idx, it in enumerate(items):
        pb = (it or {}).get("pixel_box") or {}
        x = float(pb.get("x") or 0.0)
        y_top = float(pb.get("y") or 0.0)
        bw = float(pb.get("width") or 0.0)
        bh = float(pb.get("height") or 0.0)
        if bw <= 0.0 or bh <= 0.0:
            continue

        # pixel_box uses top-left origin; CGContext uses bottom-left origin.
        y_bottom = float(h) - (y_top + bh)

        # Alternate between two high-contrast colors.
        if idx % 2 == 0:
            Quartz.CGContextSetRGBStrokeColor(ctx, 1.0, 0.2, 0.2, 0.95)  # red
        else:
            Quartz.CGContextSetRGBStrokeColor(ctx, 0.2, 0.9, 1.0, 0.95)  # cyan
        Quartz.CGContextStrokeRect(ctx, Quartz.CGRectMake(x, y_bottom, bw, bh))

    out = Quartz.CGBitmapContextCreateImage(ctx)
    if out is None:
        raise RuntimeError("failed to create overlay image from context")
    bitmap = NSBitmapImageRep.alloc().initWithCGImage_(out)
    png_data = bitmap.representationUsingType_properties_(NSPNGFileType, {})
    if png_data is None:
        raise RuntimeError("failed to encode overlay image as png")
    with open(overlay_path, "wb") as f:
        f.write(bytes(png_data))

    # Draw "text-layer" image: white background + OCR text/boxes only.
    # Use an explicit bitmap context (pixel-sized) to avoid NSImage point/pixel scaling offsets.
    text_rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bitmapFormat_bytesPerRow_bitsPerPixel_(
        None,
        int(w),
        int(h),
        8,
        4,
        True,
        False,
        NSDeviceRGBColorSpace,
        0,
        0,
        0,
    )
    if text_rep is None:
        raise RuntimeError("failed to create bitmap rep for text-layer image")
    text_ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(text_rep)
    if text_ctx is None:
        raise RuntimeError("failed to create graphics context for text-layer image")

    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(text_ctx)
    try:
        # White background.
        NSColor.whiteColor().setFill()
        NSBezierPath.fillRect_(NSMakeRect(0, 0, float(w), float(h)))

        attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_(12.0),
            NSForegroundColorAttributeName: NSColor.blackColor(),
        }

        for idx, it in enumerate(items):
            pb = (it or {}).get("pixel_box") or {}
            x = float(pb.get("x") or 0.0)
            y_top = float(pb.get("y") or 0.0)
            bw = float(pb.get("width") or 0.0)
            bh = float(pb.get("height") or 0.0)
            if bw <= 0.0 or bh <= 0.0:
                continue
            y_bottom = float(h) - (y_top + bh)

            # Draw box
            if idx % 2 == 0:
                NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.2, 0.2, 0.95).setStroke()
            else:
                NSColor.colorWithCalibratedRed_green_blue_alpha_(0.2, 0.9, 1.0, 0.95).setStroke()
            path = NSBezierPath.bezierPathWithRect_(NSMakeRect(x, y_bottom, bw, bh))
            path.setLineWidth_(1.2)
            path.stroke()

            # Draw recognized text in its box on white background.
            txt = str((it or {}).get("text") or "").strip()
            if txt:
                text_rect = NSMakeRect(
                    x + 2.0,
                    y_bottom + 2.0,
                    max(1.0, bw - 4.0),
                    max(1.0, bh - 4.0),
                )
                NSString.stringWithString_(txt).drawInRect_withAttributes_(text_rect, attrs)
    finally:
        NSGraphicsContext.restoreGraphicsState()

    text_png = text_rep.representationUsingType_properties_(NSPNGFileType, {})
    if text_png is None:
        raise RuntimeError("failed to encode text-layer image as png")
    with open(text_layer_path, "wb") as f:
        f.write(bytes(text_png))

    return {
        "out_dir": os.path.abspath(out_dir),
        "raw_screenshot_path": raw_path,
        "overlay_path": overlay_path,
        "text_layer_path": text_layer_path,
        "ocr_json_path": json_path,
    }
