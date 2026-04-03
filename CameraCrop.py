import os
import re

import nuke
from PySide2 import QtCore

MM_TO_INCH = 0.0393701
EPS = 1e-8


class ReframeBakeError(Exception):
    pass


def _msg(text):
    nuke.message(text)
    print(text)


def _ask(text):
    return bool(nuke.ask(text))


def _selected_transform_and_camera():
    sel = nuke.selectedNodes()
    transforms = [n for n in sel if n.Class() == "Transform"]
    cameras = [n for n in sel if n.Class().startswith("Camera")]

    if len(sel) != 2 or len(transforms) != 1 or len(cameras) != 1:
        raise ReframeBakeError(
            "Select exactly two nodes:\n"
            "  - one Transform\n"
            "  - one Camera (classic 3D Camera/Camera2)"
        )
    return transforms[0], cameras[0]


def _knob_xy(knob, frame=None):
    if frame is None:
        try:
            v = knob.value()
            if isinstance(v, (list, tuple)) and len(v) >= 2:
                return float(v[0]), float(v[1])
        except Exception:
            pass
    try:
        if frame is None:
            return float(knob.getValue(0)), float(knob.getValue(1))
        return float(knob.valueAt(frame, 0)), float(knob.valueAt(frame, 1))
    except Exception:
        raise ReframeBakeError("Failed to read x/y values from knob '{}'".format(knob.name()))


def _is_animated(knob):
    try:
        return bool(knob.isAnimated())
    except Exception:
        return False


def _camera_is_reading_from_file(camera):
    return "read_from_file" in camera.knobs() and bool(camera["read_from_file"].value())


def _evaluate_file_knob(node, knob_name):
    if knob_name not in node.knobs():
        return ""
    k = node[knob_name]
    try:
        p = k.evaluate()
    except Exception:
        p = k.value()
    return (p or "").strip()


def _is_ascii_fbx(path):
    with open(path, "rb") as fh:
        return b"Kaydara FBX Binary" not in fh.read(64)


def _validate_transform(node):
    required = ("translate", "center", "scale")
    for name in required:
        if name not in node.knobs():
            raise ReframeBakeError("Transform is missing '{}' knob".format(name))
        if _is_animated(node[name]):
            raise ReframeBakeError("Animated Transform knobs are not supported: '{}'".format(name))

    if "rotate" in node.knobs() and abs(float(node["rotate"].value())) > EPS:
        raise ReframeBakeError("Transform.rotate must be 0.")

    for name in ("skewX", "skewY"):
        if name in node.knobs() and abs(float(node[name].value())) > EPS:
            raise ReframeBakeError("Transform.{} must be 0.".format(name))

    sx, sy = _knob_xy(node["scale"])
    if abs(sx - sy) > 1e-6:
        raise ReframeBakeError("Transform scale must be uniform (scale.x == scale.y).")
    if sx <= 0.0:
        raise ReframeBakeError("Transform scale must be > 0.")


def _validate_camera(camera):
    for name in ("focal", "haperture", "vaperture"):
        if name not in camera.knobs():
            raise ReframeBakeError("Camera missing '{}' knob".format(name))

    # Keep these strict to avoid double transforms.
    if "win_translate" in camera.knobs():
        wx, wy = _knob_xy(camera["win_translate"])
        if abs(wx) > EPS or abs(wy) > EPS:
            raise ReframeBakeError("Camera.win_translate must be 0,0.")
    if "win_scale" in camera.knobs():
        sx, sy = _knob_xy(camera["win_scale"])
        if abs(sx - 1.0) > EPS or abs(sy - 1.0) > EPS:
            raise ReframeBakeError("Camera.win_scale must be 1,1.")
    if "winroll" in camera.knobs() and abs(float(camera["winroll"].value())) > EPS:
        raise ReframeBakeError("Camera.winroll must be 0.")




def _to_nuke_path(path):
    # Nuke file knobs are safest with POSIX separators on all platforms.
    return os.path.abspath(path).replace("\\", "/")

def _script_dir_default():
    script_path = nuke.root().name()
    if script_path and script_path != "Root":
        return os.path.dirname(script_path)
    return os.path.expanduser("~")


def _default_output_dir(camera):
    if _camera_is_reading_from_file(camera):
        src = _evaluate_file_knob(camera, "file")
        if src:
            return os.path.dirname(src)
    return _script_dir_default()


def _ask_output_folder(default_dir):
    chosen = nuke.getFilename("Choose output folder", "*", os.path.join(default_dir, "select_folder"))
    if not chosen:
        return ""
    chosen = _to_nuke_path(chosen)
    if os.path.isfile(chosen):
        chosen = os.path.dirname(chosen)
    if not os.path.exists(chosen):
        os.makedirs(chosen)
    return chosen


def _ask_output_plan(default_dir, default_base):
    out_dir = _ask_output_folder(default_dir)
    if not out_dir:
        return "", ""

    base = nuke.getInput("Base output name (example: Camera_30)", default_base)
    if base is None:
        return "", ""

    base = re.sub(r"[^0-9A-Za-z._-]+", "_", base).strip("_")
    if not base:
        raise ReframeBakeError("Base output name cannot be empty.")

    return out_dir, base


def _read_text(path):
    with open(path, "r", encoding="utf-8", errors="ignore", newline="") as fh:
        return fh.read()


def _write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _base_name(name):
    return name.split(":")[-1].split("|")[-1].strip()


def _find_matching_brace(text, open_idx):
    depth = 0
    in_string = False
    esc = False
    for i in range(open_idx, len(text)):
        ch = text[i]
        if in_string:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    raise ReframeBakeError("Malformed FBX: unmatched braces.")


def _fbx_objects(text):
    specs = {
        "models": re.compile(r'Model:\s*(\d+)\s*,\s*"Model::([^"]+)"\s*,\s*"Camera"\s*\{', re.MULTILINE),
        "attrs": re.compile(r'NodeAttribute:\s*(\d+)\s*,\s*"NodeAttribute::([^"]+)"\s*,\s*"Camera"\s*\{', re.MULTILINE),
        "curve_nodes": re.compile(r'AnimationCurveNode:\s*(\d+)\s*,\s*"([^"]+)"\s*,\s*""\s*\{', re.MULTILINE),
        "curves": re.compile(r'AnimationCurve:\s*(\d+)\s*,\s*"([^"]*)"\s*,\s*""\s*\{', re.MULTILINE),
    }
    out = {k: [] for k in specs}
    for key, pat in specs.items():
        for m in pat.finditer(text):
            open_idx = text.find("{", m.start())
            close_idx = _find_matching_brace(text, open_idx)
            out[key].append({
                "id": int(m.group(1)),
                "name": m.group(2),
                "start": m.start(),
                "end": close_idx + 1,
                "text": text[m.start():close_idx + 1],
            })
    return out


def _fbx_connections(text):
    oo = []
    op = []
    for m in re.finditer(r'C:\s*"OO"\s*,\s*(-?\d+)\s*,\s*(-?\d+)', text, re.MULTILINE):
        oo.append((int(m.group(1)), int(m.group(2))))
    for m in re.finditer(r'C:\s*"OP"\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*"([^"]+)"', text, re.MULTILINE):
        op.append((int(m.group(1)), int(m.group(2)), m.group(3)))
    return oo, op


def _choose_by_name(items, target, label):
    exact = [x for x in items if x["name"] == target]
    if len(exact) == 1:
        return exact[0]
    base = _base_name(target)
    loose = [x for x in items if _base_name(x["name"]) == base]
    if len(loose) == 1:
        return loose[0]
    raise ReframeBakeError("Could not uniquely resolve {} '{}'".format(label, target))


def _resolve_camera_blocks(text, target_model_name):
    objs = _fbx_objects(text)
    oo, _ = _fbx_connections(text)

    if not objs["models"]:
        raise ReframeBakeError("No FBX camera Model objects found.")
    if not objs["attrs"]:
        raise ReframeBakeError("No FBX camera NodeAttribute objects found.")

    model = _choose_by_name(objs["models"], target_model_name, "camera model")
    attrs_by_id = {x["id"]: x for x in objs["attrs"]}
    matched = []
    for a, b in oo:
        if a in attrs_by_id and b == model["id"]:
            matched.append(attrs_by_id[a])
        elif b in attrs_by_id and a == model["id"]:
            matched.append(attrs_by_id[b])
    matched = list({x["id"]: x for x in matched}.values())
    if len(matched) != 1:
        raise ReframeBakeError("Could not uniquely find camera attribute linked to model '{}'".format(model["name"]))
    return objs, model, matched[0]


def _get_prop(text, name, default=None):
    m = re.search(r'^[ \t]*P:\s*"{}"\s*,.*?,.*?,.*?,\s*([-+0-9.eE]+)'.format(re.escape(name)), text, re.MULTILINE)
    return float(m.group(1)) if m else default


def _set_existing_prop(text, name, value):
    val = "{:.15g}".format(float(value))
    pat = re.compile(r'(^[ \t]*P:\s*"' + re.escape(name) + r'"\s*,.*?,.*?,.*?,\s*)([-+0-9.eE]+)([^\n]*$)', re.MULTILINE)
    if not pat.search(text):
        return text
    return pat.sub(lambda m: "{}{}{}".format(m.group(1), val, m.group(3)), text, count=1)


def _set_or_add_numeric_prop(block_text, name, value):
    updated = _set_existing_prop(block_text, name, value)
    if updated != block_text:
        return updated

    m = re.search(r'(^[ \t]*Properties70:\s*\{\s*\r?\n)', block_text, re.MULTILINE)
    if not m:
        return block_text

    props_body = block_text[m.end():]
    p = re.search(r'^[ \t]*P:\s*"', props_body, re.MULTILINE)
    if p:
        indent = re.match(r'[ \t]*', props_body[p.start():]).group(0)
    else:
        indent = " " * 12

    val = "{:.15g}".format(float(value))
    line = '{}P: "{}", "Number", "", "A",{}\n'.format(indent, name, val)
    return block_text[:m.end()] + line + block_text[m.end():]


def _parse_float_array_block(array_text):
    tokens = [v.strip() for v in array_text.split(",") if v.strip()]
    if not tokens:
        return []
    out = []
    for v in tokens:
        try:
            out.append(float(v))
        except Exception:
            # If parsing is ambiguous, do not rewrite this block.
            return None
    return out


def _replace_array_values(block_text, key_name, xform):
    pat = re.compile(r'(' + re.escape(key_name) + r':\s*\*)(\d+)(\s*\{\s*a:\s*)([^}]*)((\s*)\})', re.MULTILINE | re.DOTALL)
    m = pat.search(block_text)
    if not m:
        return block_text
    vals = _parse_float_array_block(m.group(4))
    if vals is None:
        return block_text
    new_vals = ["{:.15g}".format(xform(v)) for v in vals]
    # Keep original count token untouched to avoid structural corruption.
    return pat.sub(r"\g<1>{}\g<3>{}\g<5>".format(m.group(2), ",".join(new_vals)), block_text, count=1)


def _patch_curve_block(curve_text, op):
    m = re.search(r'^[ \t]*Default:\s*([-+0-9.eE]+)', curve_text, re.MULTILINE)
    if m:
        old = float(m.group(1))
        new = op(old)
        curve_text = re.sub(
            r'(^[ \t]*Default:\s*)([-+0-9.eE]+)',
            lambda m: "{}{}".format(m.group(1), "{:.15g}".format(new)),
            curve_text,
            count=1,
            flags=re.MULTILINE,
        )
    curve_text = _replace_array_values(curve_text, "KeyValueFloat", op)
    curve_text = _replace_array_values(curve_text, "KeyValueDouble", op)
    return curve_text


def _patch_property_everywhere(full_text, target_model_name, property_name, op):
    objs, _, attr = _resolve_camera_blocks(full_text, target_model_name)
    _, op_conns = _fbx_connections(full_text)
    curve_nodes = {x["id"]: x for x in objs["curve_nodes"]}
    curves = {x["id"]: x for x in objs["curves"]}

    replacements = []

    old_attr_val = _get_prop(attr["text"], property_name, 0.0)
    attr_rewrite = _set_or_add_numeric_prop(attr["text"], property_name, op(old_attr_val))
    if attr_rewrite != attr["text"]:
        replacements.append((attr["start"], attr["end"], attr_rewrite))

    curve_node = None
    for src_id, dst_id, prop_name in op_conns:
        if dst_id == attr["id"] and prop_name == property_name and src_id in curve_nodes:
            curve_node = curve_nodes[src_id]
            break

    if curve_node is not None:
        old_d = _get_prop(curve_node["text"], "d|" + property_name, None)
        if old_d is not None:
            replacements.append((curve_node["start"], curve_node["end"], _set_existing_prop(curve_node["text"], "d|" + property_name, op(old_d))))

        curve_obj = None
        want = "d|" + property_name
        for src_id, dst_id, prop_name in op_conns:
            if dst_id == curve_node["id"] and prop_name == want and src_id in curves:
                curve_obj = curves[src_id]
                break
        if curve_obj is not None:
            replacements.append((curve_obj["start"], curve_obj["end"], _patch_curve_block(curve_obj["text"], op)))

    for s, e, r in sorted(replacements, key=lambda x: x[0], reverse=True):
        full_text = full_text[:s] + r + full_text[e:]
    return full_text


def _property_curve_links_for_object(full_text, object_id, property_name):
    objs = _fbx_objects(full_text)
    _, op_conns = _fbx_connections(full_text)
    curve_nodes = {x["id"]: x for x in objs["curve_nodes"]}
    curves = {x["id"]: x for x in objs["curves"]}

    curve_node_id = None
    curve_id = None

    for src_id, dst_id, prop_name in op_conns:
        if dst_id == object_id and prop_name == property_name and src_id in curve_nodes:
            curve_node_id = src_id
            break

    if curve_node_id is not None:
        want = "d|" + property_name
        for src_id, dst_id, prop_name in op_conns:
            if dst_id == curve_node_id and prop_name == want and src_id in curves:
                curve_id = src_id
                break

    return curve_node_id, curve_id


def _camera_property_curve_links(full_text, target_model_name, property_name):
    _, _, attr = _resolve_camera_blocks(full_text, target_model_name)
    curve_node_id, curve_id = _property_curve_links_for_object(full_text, attr["id"], property_name)
    return attr["id"], curve_node_id, curve_id


def _camera_property_has_animation(full_text, target_model_name, property_name):
    _, curve_node_id, curve_id = _camera_property_curve_links(full_text, target_model_name, property_name)
    return curve_node_id is not None or curve_id is not None


def _remove_property_animation_links_for_object(full_text, object_id, property_name):
    curve_node_id, curve_id = _property_curve_links_for_object(full_text, object_id, property_name)
    if curve_node_id is None:
        return full_text

    pat_obj = re.compile(
        r'^\s*C:\s*"OP"\s*,\s*{}\s*,\s*{}\s*,\s*"{}"\s*\r?\n?'.format(curve_node_id, object_id, re.escape(property_name)),
        re.MULTILINE,
    )
    full_text = pat_obj.sub("", full_text)

    if curve_id is not None:
        pat_curve = re.compile(
            r'^\s*C:\s*"OP"\s*,\s*{}\s*,\s*{}\s*,\s*"{}"\s*\r?\n?'.format(curve_id, curve_node_id, re.escape("d|" + property_name)),
            re.MULTILINE,
        )
        full_text = pat_curve.sub("", full_text)

    return full_text


def _remove_camera_property_animation_links(full_text, target_model_name, property_name):
    attr_id, _, _ = _camera_property_curve_links(full_text, target_model_name, property_name)
    return _remove_property_animation_links_for_object(full_text, attr_id, property_name)


def _force_static_camera_properties(full_text, target_model_name, properties):
    for prop in properties:
        full_text = _remove_camera_property_animation_links(full_text, target_model_name, prop)
    return full_text


def _remove_static_camera_animation(full_text, target_model_name):
    _, model, attr = _resolve_camera_blocks(full_text, target_model_name)

    for prop in ("Lcl Translation", "Lcl Rotation", "Lcl Scaling"):
        full_text = _remove_property_animation_links_for_object(full_text, model["id"], prop)

    for prop in ("FocalLength", "FilmOffsetX", "FilmOffsetY", "NearPlane", "FarPlane", "Roll", "FieldOfView", "FieldOfViewX", "FieldOfViewY"):
        full_text = _remove_property_animation_links_for_object(full_text, attr["id"], prop)

    return full_text


def _compute_reframe(transform, camera):
    tx, ty = _knob_xy(transform["translate"])
    cx, cy = _knob_xy(transform["center"])
    sx, sy = _knob_xy(transform["scale"])
    if abs(sx - sy) > 1e-6:
        raise ReframeBakeError("Transform scale must be uniform.")
    scale = sx
    if scale <= 0:
        raise ReframeBakeError("Transform scale must be > 0.")

    fmt = transform.input(0).format() if transform.input(0) else nuke.root().format()
    frame_w, frame_h = float(fmt.width()), float(fmt.height())
    if frame_w <= 0 or frame_h <= 0:
        raise ReframeBakeError("Invalid frame format.")

    sensor_w_mm = float(camera["haperture"].value())
    sensor_h_mm = float(camera["vaperture"].value())
    focal_mm = float(camera["focal"].value())

    shift_x_px = tx + (1.0 - scale) * (cx - frame_w * 0.5)
    shift_y_px = ty + (1.0 - scale) * (cy - frame_h * 0.5)

    delta_u = (shift_x_px / frame_w)
    delta_v = (shift_y_px / frame_h)

    return {
        "tx": tx,
        "ty": ty,
        "cx": cx,
        "cy": cy,
        "scale": scale,
        "frame_w": frame_w,
        "frame_h": frame_h,
        "sensor_w_mm": sensor_w_mm,
        "sensor_h_mm": sensor_h_mm,
        "focal_mm": focal_mm,
        "shift_x_px": shift_x_px,
        "shift_y_px": shift_y_px,
        "delta_u": delta_u,
        "delta_v": delta_v,
        "delta_hfo_in": delta_u * (sensor_w_mm * MM_TO_INCH),
        "delta_vfo_in": delta_v * (sensor_h_mm * MM_TO_INCH),
    }


def _export_camera_to_ascii_fbx(camera_node, out_path, static_only):
    temp_scene = None
    temp_write = None
    try:
        temp_scene = nuke.nodes.Scene(inputs=[camera_node])
        temp_write = nuke.nodes.WriteGeo(inputs=[temp_scene])
        temp_write["file"].setValue(_to_nuke_path(out_path))

        if "file_type" in temp_write.knobs():
            try:
                temp_write["file_type"].setValue("fbx")
            except Exception:
                pass

        for knob, val in (("writeCameras", True), ("writeGeometries", False), ("writeLights", False),
                          ("writeAxes", False), ("writePointClouds", False), ("asciiFileFormat", True)):
            if knob in temp_write.knobs():
                temp_write[knob].setValue(val)

        if static_only:
            fr = int(nuke.frame())
            nuke.execute(temp_write.name(), fr, fr)
        else:
            first = int(nuke.root()["first_frame"].value())
            last = int(nuke.root()["last_frame"].value())
            nuke.execute(temp_write.name(), first, last)
    finally:
        for n in (temp_write, temp_scene):
            if n is not None:
                try:
                    nuke.delete(n)
                except Exception:
                    pass


def _camera_is_static_in_nuke(camera):
    knobs = ["translate", "rotate", "scaling", "uniform_scale", "pivot", "focal", "haperture", "vaperture"]
    for k in knobs:
        if k in camera.knobs() and _is_animated(camera[k]):
            return False
    return True


def _new_output_names(base_name):
    clean = re.sub(r"[^0-9A-Za-z._-]+", "_", base_name).strip("_") or "camera"
    return {
        "nuke": clean + "_nuke.fbx",
        "maya": clean + "_maya.fbx",
        "unreal": clean + "_unreal.fbx",
    }


def _resolve_target_model_name(camera):
    if "fbx_node_name" in camera.knobs():
        try:
            s = str(camera["fbx_node_name"].value()).strip()
            if s:
                return s
        except Exception:
            pass
    return camera.name()


def _confirm_overwrite(paths):
    existing = [p for p in paths if os.path.exists(p)]
    if not existing:
        return True
    return _ask("The following files already exist and will be overwritten:\n\n{}\n\nContinue?".format("\n".join(existing)))


PRODUCER_NAMES = {
    "Producer Perspective",
    "Producer Top",
    "Producer Bottom",
    "Producer Left",
    "Producer Right",
    "Producer Front",
    "Producer Back",
}


def _get_enum_names(knob):
    try:
        return list(knob.values())
    except Exception:
        return []


def _select_camera_node(nuke_cam_node, wanted_camera_name, max_tries=20, delay_ms=100):
    node_name = nuke_cam_node.fullName()
    state = {"tries": 0}

    def try_finish():
        state["tries"] += 1

        cam_now = nuke.toNode(node_name)
        if cam_now is None:
            nuke.tprint("FBX setup aborted: node no longer exists: {}".format(node_name))
            return

        try:
            cam_now.forceValidate()
        except Exception:
            pass

        try:
            if "reload" in cam_now.knobs():
                try:
                    cam_now["reload"].execute()
                except Exception:
                    pass

            names = _get_enum_names(cam_now["fbx_node_name"])
        except Exception as exc:
            if state["tries"] < max_tries:
                QtCore.QTimer.singleShot(delay_ms, try_finish)
            else:
                nuke.warning("Could not inspect FBX camera list.\n{}".format(exc))
            return

        if wanted_camera_name in names:
            try:
                cam_now["fbx_node_name"].setValue(wanted_camera_name)
            except Exception:
                try:
                    cam_now["fbx_node_name"].setValue(names.index(wanted_camera_name))
                except Exception as exc:
                    nuke.warning(
                        "Found camera '{}', but could not set fbx_node_name.\n{}".format(
                            wanted_camera_name, exc
                        )
                    )
                    return

            try:
                if "reload" in cam_now.knobs():
                    cam_now["reload"].execute()
            except Exception:
                pass

            nuke.tprint("Selected FBX camera: {}".format(wanted_camera_name))
            return

        if state["tries"] < max_tries:
            QtCore.QTimer.singleShot(delay_ms, try_finish)
        else:
            non_producer = [n for n in names if n not in PRODUCER_NAMES]
            nuke.warning(
                "Could not find FBX camera '{}'.\n\nAvailable names:\n{}\n\nNon-producer names:\n{}".format(
                    wanted_camera_name,
                    "\n".join(names) if names else "(none)",
                    "\n".join(non_producer) if non_producer else "(none)",
                )
            )

    QtCore.QTimer.singleShot(delay_ms, try_finish)
    return nuke_cam_node


def _create_imported_camera(file_path, target_name, ref_node):
    cam = nuke.nodes.Camera2()
    try:
        cam.setName(ref_node.name() + "_reframed")
    except Exception:
        pass
    try:
        cam.setXpos(ref_node.xpos() + 100)
        cam.setYpos(ref_node.ypos() + 100)
    except Exception:
        pass

    if "read_from_file" in cam.knobs():
        cam["read_from_file"].setValue(True)
    cam["file"].setValue(_to_nuke_path(file_path))
    if "reload" in cam.knobs():
        try:
            cam["reload"].execute()
        except Exception:
            pass

    _select_camera_node(cam, target_name)
    return cam




def _patch_property_static(full_text, target_model_name, property_name, op):
    objs, _, attr = _resolve_camera_blocks(full_text, target_model_name)
    old_val = _get_prop(attr["text"], property_name, 0.0)
    new_val = op(old_val)

    attr_new = _set_or_add_numeric_prop(attr["text"], property_name, new_val)
    if attr_new != attr["text"]:
        full_text = full_text[:attr["start"]] + attr_new + full_text[attr["end"]:]

    full_text = _remove_camera_property_animation_links(full_text, target_model_name, property_name)
    return full_text


def _unreal_film_offset_from_reframe(comp):
    # Unreal-target mapping for FBX camera offsets.
    # Units are inches because FBX FilmOffsetX/Y camera properties are in inches.
    # Y is inverted relative to Nuke/Maya reframe math to match Unreal import behavior.
    return {
        "properties": ("FilmOffsetX", "FilmOffsetY"),
        "delta_x": comp["delta_hfo_in"],
        "delta_y": -comp["delta_vfo_in"],
        "units": "in",
    }


def _set_default_camera_in_fbx_text(source_text, camera_name):
    if not camera_name:
        return source_text

    pat = re.compile(r'(^[ \t]*P:\s*"DefaultCamera"\s*,\s*"KString"\s*,\s*""\s*,\s*""\s*,\s*")[^"]*(".*$)', re.MULTILINE)
    if pat.search(source_text):
        safe_name = camera_name.replace("\\", "\\\\").replace('"', '\\"')
        return pat.sub(lambda m: '{}{}{}'.format(m.group(1), safe_name, m.group(2)), source_text, count=1)

    block = re.search(r'(GlobalSettings:\s*\{\s*\r?\n(?:.*?\r?\n)*?[ \t]*Properties70:\s*\{\s*\r?\n)', source_text, re.DOTALL)
    if not block:
        return source_text

    return (
        source_text[:block.end()] +
        '        P: "DefaultCamera", "KString", "", "", "{}"\n'.format(camera_name) +
        source_text[block.end():]
    )


def _apply_reframe_to_fbx_text(source_text, target_model_name, comp, dcc, force_static=False):
    scale = comp["scale"]
    text = source_text

    if force_static:
        apply_prop = lambda t, p, fn: _patch_property_static(t, target_model_name, p, fn)
    else:
        apply_prop = lambda t, p, fn: _patch_property_everywhere(t, target_model_name, p, fn)

    text = apply_prop(text, "FocalLength", lambda old: old * scale)

    if dcc == "nuke":
        # Nuke consumes win_translate in normalized filmback units (u/v), while FBX stores FilmOffset in inches.
        # Convert desired win_translate deltas to FBX FilmOffset deltas using the camera filmback size.
        dx = _nuke_win_translate_x_to_film_offset_in(-comp["delta_u"], comp["sensor_w_mm"])
        dy = _nuke_win_translate_y_to_film_offset_in(-comp["delta_v"], comp["sensor_w_mm"])
        # FBX FilmOffsetX/Y are inch values; after import, Nuke converts them back to normalized win_translate.
        text = apply_prop(text, "FilmOffsetX", lambda old: old + dx)
        text = apply_prop(text, "FilmOffsetY", lambda old: old + dy)

    elif dcc == "maya":
        dx = comp["delta_hfo_in"]
        dy = comp["delta_vfo_in"]
        # Maya consumes FBX FilmOffsetX/Y as film offset inches.
        text = apply_prop(text, "FilmOffsetX", lambda old: old + dx)
        text = apply_prop(text, "FilmOffsetY", lambda old: old + dy)

    else:  # unreal
        dx = comp["delta_hfo_in"]
        dy = -comp["delta_vfo_in"]
        # Unreal also consumes FBX FilmOffsetX/Y as inches, with opposite Y sign convention.
        text = apply_prop(text, "FilmOffsetX", lambda old: old + dx)
        text = apply_prop(text, "FilmOffsetY", lambda old: old + dy)

    if force_static:
        text = _remove_static_camera_animation(text, target_model_name)
    return text


def _dcc_adjustments(comp):
    focal_old = comp["focal_mm"]
    focal_new = focal_old * comp["scale"]
    unreal_offsets = _unreal_film_offset_from_reframe(comp)
    return {
        "nuke": {
            "focal_old_mm": focal_old,
            "focal_new_mm": focal_new,
            "delta_x_win_translate": comp["delta_u"],
            "delta_y_win_translate": comp["delta_v"],
        },
        "maya": {
            "focal_old_mm": focal_old,
            "focal_new_mm": focal_new,
            "delta_x_in": comp["delta_hfo_in"],
            "delta_y_in": comp["delta_vfo_in"],
        },
        "unreal": {
            "focal_old_mm": focal_old,
            "focal_new_mm": focal_new,
            "delta_x": unreal_offsets["delta_x"],
            "delta_y": unreal_offsets["delta_y"],
            "units": unreal_offsets["units"],
        },
    }


def _nuke_win_translate_x_to_film_offset_in(delta_win_translate_x, haperture_mm):
    # Nuke's win_translate.x normalization uses half the horizontal aperture:
    #   win_translate.x = delta_x_mm / (haperture_mm / 2)
    # => delta_x_mm = win_translate.x * (haperture_mm / 2)
    return delta_win_translate_x * (haperture_mm * 0.5 * MM_TO_INCH)


def _nuke_win_translate_y_to_film_offset_in(delta_win_translate_y, haperture_mm):
    # Nuke's win_translate.y normalization also uses horizontal aperture:
    #   win_translate.y = delta_y_mm / haperture_mm
    # => delta_y_mm = win_translate.y * haperture_mm
    return delta_win_translate_y * (haperture_mm * MM_TO_INCH)


def _summary_text(transform, camera, comp, out_dir, outs, source_mode, source_fbx):
    dcc = _dcc_adjustments(comp)
    lines = [
        "CameraCrop reframe bake",
        "----------------------",
        "Transform: {}".format(transform.name()),
        "Camera:    {}".format(camera.name()),
        "Mode:      {}".format(source_mode),
        "Source FBX: {}".format(source_fbx if source_fbx else "(will export from camera node)"),
        "",
        "Frame size: {} x {}".format(int(comp["frame_w"]), int(comp["frame_h"])),
        "Translate: ({:.6f}, {:.6f}) px".format(comp["tx"], comp["ty"]),
        "Center:    ({:.6f}, {:.6f}) px".format(comp["cx"], comp["cy"]),
        "Scale:     {:.8f}".format(comp["scale"]),
        "",
        "Per-DCC adjustments:",
        "  Nuke   -> Focal: {:.6f} -> {:.6f} mm, win_translate delta: ({:.10f}, {:.10f}) normalized".format(
            dcc["nuke"]["focal_old_mm"], dcc["nuke"]["focal_new_mm"], dcc["nuke"]["delta_x_win_translate"], dcc["nuke"]["delta_y_win_translate"]
        ),
        "  Maya   -> Focal: {:.6f} -> {:.6f} mm, FilmOffset delta: ({:.10f}, {:.10f}) in".format(
            dcc["maya"]["focal_old_mm"], dcc["maya"]["focal_new_mm"], dcc["maya"]["delta_x_in"], dcc["maya"]["delta_y_in"]
        ),
        "  Unreal -> Focal: {:.6f} -> {:.6f} mm, FilmOffset delta: ({:.10f}, {:.10f}) {} (FBX FilmOffsetX/Y mapping, Y inverted)".format(
            dcc["unreal"]["focal_old_mm"], dcc["unreal"]["focal_new_mm"], dcc["unreal"]["delta_x"], dcc["unreal"]["delta_y"], dcc["unreal"]["units"]
        ),
        "",
        "Output folder:",
        out_dir,
        "",
        "Files:",
        "  - {}".format(outs["nuke"]),
        "  - {}".format(outs["maya"]),
        "  - {}".format(outs["unreal"]),
    ]
    return "\n".join(lines)


def bake_selected_transform_into_cameras():
    transform, camera = _selected_transform_and_camera()
    selected_camera_name = camera.name()
    _validate_transform(transform)
    _validate_camera(camera)

    source_fbx = ""
    source_mode = "manual camera export"
    if _camera_is_reading_from_file(camera):
        source_fbx = _evaluate_file_knob(camera, "file")
        if not source_fbx:
            raise ReframeBakeError("Camera is set to read from file, but file path is empty.")
        if not os.path.exists(source_fbx):
            raise ReframeBakeError("FBX file does not exist:\n{}".format(source_fbx))
        if not source_fbx.lower().endswith(".fbx"):
            raise ReframeBakeError("Camera file must be .fbx")
        if not _is_ascii_fbx(source_fbx):
            raise ReframeBakeError("Only ASCII FBX is supported for read-from-file cameras.")
        source_mode = "patch existing ASCII FBX"

    comp = _compute_reframe(transform, camera)

    default_dir = _default_output_dir(camera)
    suggested_base = os.path.splitext(os.path.basename(source_fbx))[0] if source_fbx else camera.name()
    out_dir, base_name = _ask_output_plan(default_dir, suggested_base)
    if not out_dir:
        return

    output_names = _new_output_names(base_name)
    output_paths = {k: _to_nuke_path(os.path.join(out_dir, v)) for k, v in output_names.items()}

    summary = _summary_text(transform, camera, comp, out_dir, output_names, source_mode, source_fbx)
    print(summary)
    if not _ask(summary + "\n\nProceed?"):
        return

    if not _confirm_overwrite(list(output_paths.values())):
        return

    target_model_name = _resolve_target_model_name(camera)

    if source_fbx:
        source_text = _read_text(source_fbx)
        source_is_static = not any([
            _camera_property_has_animation(source_text, target_model_name, "FocalLength"),
            _camera_property_has_animation(source_text, target_model_name, "FilmOffsetX"),
            _camera_property_has_animation(source_text, target_model_name, "FilmOffsetY"),
        ])
    else:
        temp_src = _to_nuke_path(os.path.join(out_dir, output_names["nuke"] + ".tmp_export_source.fbx"))
        static_only = _camera_is_static_in_nuke(camera)
        _export_camera_to_ascii_fbx(camera, temp_src, static_only=static_only)
        if not os.path.exists(temp_src):
            raise ReframeBakeError("Failed to export source FBX from camera.")
        if not _is_ascii_fbx(temp_src):
            raise ReframeBakeError("Exported camera FBX is binary; expected ASCII.")
        source_text = _read_text(temp_src)
        try:
            os.remove(temp_src)
        except Exception:
            pass

    force_static_rewrite = source_is_static if source_fbx else _camera_is_static_in_nuke(camera)

    for dcc in ("nuke", "maya", "unreal"):
        patched = _apply_reframe_to_fbx_text(source_text, target_model_name, comp, dcc, force_static=force_static_rewrite)
        if dcc == "nuke":
            patched = _set_default_camera_in_fbx_text(patched, selected_camera_name)
        _write_text(output_paths[dcc], patched)

    imported = _create_imported_camera(output_paths["nuke"], selected_camera_name, camera)

    _msg(
        "Success.\n\n"
        "Created FBX files:\n"
        "- {}\n- {}\n- {}\n\n"
        "Imported verification camera: {}"
        .format(output_paths["nuke"], output_paths["maya"], output_paths["unreal"], imported.name())
    )


try:
    bake_selected_transform_into_cameras()
except ReframeBakeError as exc:
    _msg("Reframe bake failed:\n\n{}".format(exc))
    raise
except Exception as exc:
    _msg("Unexpected error:\n\n{}".format(exc))
    raise
