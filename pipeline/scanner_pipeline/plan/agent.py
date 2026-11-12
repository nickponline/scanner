"""Gemini 4 Argon agent loop that produces the floor plan + architectural/furniture model (REQUIREMENTS §5.8).

Argon gets tools that render/measure the reconstruction; it proposes a model, sees it overlaid on the scan,
fixes problems flagged by the validator, and submits. All coordinates it uses come from tool outputs.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import cv2
import numpy as np

from .build import ROOM_TYPES, overlay, validate
from .geometry import (Scene, jpg_bytes, object_candidates, photos_near, png_bytes, render_elevation, render_topdown,
                       wall_candidates, wall_evidence)

log = logging.getLogger("scanner.plan")
MODEL = os.environ.get("ARGON_MODEL", "gemini-4-argon")

SYSTEM = """You are an expert architectural surveyor producing an accurate as-built floor plan and a simple 3D
building model from a 3D scan of a home (single storey). You work ONLY through the tools provided.

Coordinate system: metres, Z up, floor at z=0, X to the right and Y up in every top-down image. Grid lines and
axis labels in images are world coordinates in metres — read positions off them. The scan has been rotated so
most walls are parallel to the X or Y axis. Ceiling height is given in the overview.

Method:
1. Call get_overview. Study the top-down colour render, the wall-evidence render and the wall candidate list.
2. Inspect details with render_topdown (zoom with bbox, cut heights), render_elevation along walls (doors =
   gaps reaching the floor with wall above; windows = openings with a sill), and photos (find_photos / view_photo)
   to identify room uses, doors, windows and furniture.
3. Propose the full model with check_model. Fix every error and consider every warning; iterate until the
   overlay matches the scan closely. Prefer candidate wall coordinates (they are measured), snap shared corners
   exactly, keep walls axis-aligned unless the scan clearly shows otherwise.
4. Call submit_model with the final model.

Model JSON (all coordinates in metres):
{"walls":[{"id":"w1","start":[x,y],"end":[x,y],"thickness":0.12,"exterior":true}],
 "rooms":[{"id":"r1","name":"Kitchen","type":"<one of TYPES>","polygon":[[x,y],...]}],
 "openings":[{"id":"d1","type":"door|window|opening","wall_id":"w1","center":[x,y],"width":0.82,"height":2.03,
              "sill_z":0.0,"swing":{"hinge":"start|end","side":"left|right"}}],
 "furniture":[{"id":"f1","label":"sofa","center":[x,y,z_mid],"size":[sx,sy,sz],"yaw_deg":0,"room_id":"r1"}]}
Room polygons follow the inner faces of walls (counter-clockwise), rooms must not overlap, every door/window
must lie on a wall. Furniture: label the clear, large items (sofa, bed, table, chairs, cabinets, kitchen counters,
wardrobe, desk, appliances...) as oriented boxes; use object candidates for their measured size/position.
TYPES = """ + ", ".join(ROOM_TYPES)


TOOLS = [
    {"name": "get_overview", "description": "Scene summary, wall candidates, object candidates, and two top-down images (colour; wall evidence). Call first.",
     "parameters_json_schema": {"type": "object", "properties": {}}},
    {"name": "render_topdown", "description": "Orthographic top-down render of scan points between z_min and z_max (highest point wins). Optional bbox zooms in.",
     "parameters_json_schema": {"type": "object", "properties": {
         "z_min": {"type": "number"}, "z_max": {"type": "number"},
         "bbox": {"type": "array", "items": {"type": "number"}, "description": "[x_min, y_min, x_max, y_max]"},
         "mode": {"type": "string", "enum": ["color", "walls", "height"]},
         "show_cameras": {"type": "boolean"}}, "required": ["z_min", "z_max"]}},
    {"name": "render_elevation", "description": "Side view of scan points within ±0.12 m of the line start→end (e.g. along a wall) to see doors, windows and openings with heights.",
     "parameters_json_schema": {"type": "object", "properties": {
         "start": {"type": "array", "items": {"type": "number"}}, "end": {"type": "array", "items": {"type": "number"}}},
         "required": ["start", "end"]}},
    {"name": "find_photos", "description": "Photos whose camera looks at the point (x, y), best first. Returns ids and camera positions.",
     "parameters_json_schema": {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"},
                                                                 "k": {"type": "integer"}}, "required": ["x", "y"]}},
    {"name": "view_photo", "description": "Look at a photo by id.",
     "parameters_json_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "check_model", "description": "Validate a proposed model; returns errors, warnings, areas, wall evidence support and an overlay image of the model on the scan.",
     "parameters_json_schema": {"type": "object", "properties": {"model_json": {"type": "string", "description": "The model as a JSON string"}},
                                "required": ["model_json"]}},
    {"name": "submit_model", "description": "Submit the final model. Rejected if it has validation errors.",
     "parameters_json_schema": {"type": "object", "properties": {"model_json": {"type": "string"},
                                                                 "notes": {"type": "string"}}, "required": ["model_json"]}},
]


class PlanAgent:
    def __init__(self, sc: Scene, api_key: str, out_dir: Path, max_turns=40, budget_s=1800):
        from google import genai

        self.sc = sc
        self.client = genai.Client(api_key=api_key)
        self.out = out_dir
        (out_dir / "agent").mkdir(parents=True, exist_ok=True)
        self.max_turns = max_turns
        self.budget_s = budget_s
        self.ev = wall_evidence(sc)
        self.walls = wall_candidates(sc)
        self.objects = object_candidates(sc, self.walls)
        self.final = None
        self.last_valid = None
        self.transcript: list[dict] = []
        self.n_img = 0
        self.usage = {"prompt": 0, "output": 0, "thoughts": 0}

    # ------------------------------------------------------------------ tools
    def _img(self, img, kind="png"):
        self.n_img += 1
        p = self.out / "agent" / f"{self.n_img:03d}.{ 'png' if kind == 'png' else 'jpg'}"
        data = png_bytes(img) if kind == "png" else jpg_bytes(img)
        p.write_bytes(data)
        return ("image/png" if kind == "png" else "image/jpeg", data)

    def t_get_overview(self):
        top, _ = render_topdown(self.sc, 0.0, self.sc.ceiling - 0.15, mode="color", cameras=True)
        walls, _ = render_topdown(self.sc, 0.3, min(2.1, self.sc.ceiling - 0.2), mode="walls")
        info = {"ceiling_height": round(float(self.sc.ceiling), 3),
                "extent": {"x": [round(float(self.sc.lo[0]), 2), round(float(self.sc.hi[0]), 2)],
                           "y": [round(float(self.sc.lo[1]), 2), round(float(self.sc.hi[1]), 2)]},
                "n_photos": len(self.sc.cameras),
                "wall_candidates": self.walls,
                "object_candidates": self.objects,
                "notes": "image 1: colour top-down (red dots = photo positions). image 2: vertical surfaces (dark) between 0.3 and 2.1 m."}
        return info, [self._img(top), self._img(walls)]

    def t_render_topdown(self, z_min, z_max, bbox=None, mode="color", show_cameras=False):
        img, _ = render_topdown(self.sc, z_min, z_max, bbox=bbox, mode=mode, cameras=show_cameras)
        return {"ok": True}, [self._img(img)]

    def t_render_elevation(self, start, end):
        img = render_elevation(self.sc, start, end)
        L = float(np.linalg.norm(np.subtract(end, start)))
        return {"length_m": round(L, 3), "viewed_from": "left side of start→end"}, [self._img(img)]

    def t_find_photos(self, x, y, k=4):
        cams = photos_near(self.sc, x, y, k)
        return {"photos": [{"id": c["id"], "camera_xy": [round(float(c["center"][0]), 2), round(float(c["center"][1]), 2)],
                            "looking_dir_xy": [round(float(c["c2w"][0, 2]), 2), round(float(c["c2w"][1, 2]), 2)]} for c in cams]}, []

    def t_view_photo(self, id):
        cam = next((c for c in self.sc.cameras if c["id"] == id), None)
        if cam is None:
            return {"error": f"no photo {id}"}, []
        img = cv2.imread(str(cam["image_path"]))
        s = 1024 / max(img.shape[:2])
        img = cv2.resize(img, (int(img.shape[1] * s), int(img.shape[0] * s)), interpolation=cv2.INTER_AREA)
        # rotate upright using the camera's up vector
        up_img = cam["c2w"][:3, :3].T @ np.array([0, 0, 1.0])  # world up in camera coords (OpenCV: -y is up)
        ang = np.degrees(np.arctan2(up_img[0], -up_img[1]))
        k = int(np.round(ang / 90)) % 4
        if k:
            img = np.ascontiguousarray(np.rot90(img, k=k))
        return {"id": id, "camera_xy": [round(float(cam["center"][0]), 2), round(float(cam["center"][1]), 2)]}, [self._img(img, "jpg")]

    def _parse(self, model_json):
        try:
            m = json.loads(model_json) if isinstance(model_json, str) else model_json
            assert isinstance(m, dict)
            return m, None
        except Exception as e:  # noqa: BLE001
            return None, f"model_json is not valid JSON: {e}"

    def t_check_model(self, model_json):
        m, err = self._parse(model_json)
        if err:
            return {"errors": [err]}, []
        errs, warns, derived = validate(m, self.sc, self.ev)
        if not errs:
            self.last_valid = m
        return {"errors": errs, "warnings": warns, "derived": derived}, [self._img(overlay(m, self.sc))]

    def t_submit_model(self, model_json, notes=""):
        m, err = self._parse(model_json)
        if err:
            return {"accepted": False, "errors": [err]}, []
        errs, warns, derived = validate(m, self.sc, self.ev)
        if errs:
            return {"accepted": False, "errors": errs, "warnings": warns}, []
        self.final = {"model": m, "notes": notes, "derived": derived, "warnings": warns}
        return {"accepted": True}, []

    # ------------------------------------------------------------------ loop
    def run(self) -> dict:
        from google.genai import types

        tools = [types.Tool(function_declarations=[types.FunctionDeclaration(**t) for t in TOOLS])]
        cfg = types.GenerateContentConfig(system_instruction=SYSTEM, tools=tools,
                                          automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                                          thinking_config=types.ThinkingConfig(thinking_level="high"))
        contents = [types.Content(role="user", parts=[types.Part.from_text(
            text="Produce the floor plan and building model for this scan. Start with get_overview.")])]
        t0 = time.time()
        turn = 0
        nudged = False
        while turn < self.max_turns and self.final is None:
            turn += 1
            resp = self._generate(contents, cfg)
            um = resp.usage_metadata
            if um:
                self.usage["prompt"] += um.prompt_token_count or 0
                self.usage["output"] += um.candidates_token_count or 0
                self.usage["thoughts"] += um.thoughts_token_count or 0
            cand = resp.candidates[0]
            contents.append(cand.content)
            calls = [p.function_call for p in (cand.content.parts or []) if p.function_call]
            text = "".join(p.text or "" for p in (cand.content.parts or []) if p.text and not p.thought)
            self.transcript.append({"turn": turn, "text": text[:4000], "calls": [{"name": c.name, "args": _short(c.args)} for c in calls]})
            log.info("argon turn %d: %s %s", turn, [c.name for c in calls], text[:200].replace("\n", " "))
            if not calls:
                contents.append(types.Content(role="user", parts=[types.Part.from_text(
                    text="Continue using the tools. When the model is right, call submit_model.")]))
                continue
            parts = []
            for c in calls:
                fn = getattr(self, f"t_{c.name}", None)
                try:
                    result, images = fn(**(c.args or {})) if fn else ({"error": f"unknown tool {c.name}"}, [])
                except Exception as e:  # noqa: BLE001
                    log.exception("tool %s failed", c.name)
                    result, images = {"error": f"{type(e).__name__}: {e}"}, []
                fr_parts = [types.FunctionResponsePart(inline_data=types.FunctionResponseBlob(mime_type=mt, data=d))
                            for mt, d in images] if images else None
                parts.append(types.Part(function_response=types.FunctionResponse(
                    id=c.id, name=c.name, response=_jsonable(result), parts=fr_parts)))
            elapsed = time.time() - t0
            if not nudged and (elapsed > self.budget_s * 0.75 or turn >= self.max_turns - 4):
                parts.append(types.Part.from_text(text="Time/turn budget nearly used: finalise and call submit_model now."))
                nudged = True
            contents.append(types.Content(role="user", parts=parts))
            if elapsed > self.budget_s:
                break
        (self.out / "agent" / "transcript.json").write_text(json.dumps(self.transcript, indent=1))
        if self.final is None and self.last_valid is not None:
            errs, warns, derived = validate(self.last_valid, self.sc, self.ev)
            self.final = {"model": self.last_valid, "notes": "auto-submitted last valid proposal", "derived": derived,
                          "warnings": warns}
        if self.final is None:
            raise RuntimeError("Argon did not produce a valid model")
        self.final.update({"turns": turn, "usage": self.usage, "elapsed_s": round(time.time() - t0, 1)})
        return self.final

    def _generate(self, contents, cfg):
        delay = 5
        for attempt in range(6):
            try:
                return self.client.models.generate_content(model=MODEL, contents=contents, config=cfg)
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if attempt == 5 or not any(k in msg for k in ("429", "500", "503", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE")):
                    raise
                log.warning("argon retry %d after %s", attempt + 1, msg[:200])
                time.sleep(delay)
                delay *= 2
        raise RuntimeError("unreachable")


def _short(a):
    s = json.dumps(a, default=str) if a is not None else ""
    return s[:1500]


def _jsonable(x):
    return json.loads(json.dumps(x, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o)))
