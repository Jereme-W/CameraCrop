# Nuke 14.1v5 / classic 3D Camera workflow
#
# Select exactly:
#   - one Transform node
#   - one Camera node
#
# Behavior:
#   A) If the Camera is already reading from an ASCII FBX:
#      - writes a sibling patched FBX
#      - optionally repoints/reloads the selected Camera
#
#   B) If the Camera is NOT reading from a file:
#      - prompts for a save path
#      - exports a brand-new ASCII FBX via Scene + WriteGeo
#      - if the camera is static, exports a single frame only
#      - patches the exported FBX
#      - creates a new imported Camera node in Nuke pointing at that FBX
#
# Mathematical scope:
#   - uniform scale only
#   - no rotate / skew
#   - static Transform values
#   - camera win_translate = 0,0 ; win_scale = 1,1 ; winroll = 0
#
# Units:
#   - focal length in mm
#   - film offsets in inches

import nuke
import os
import re

MM_TO_INCH = 0.0393701
EPS = 1e-8


class ReframeBakeError(Exception):
    pass


def _msg(text):
    nuke.message(text)
    print(text)


def _selected_transform_and_camera():
    sel = nuke.selectedNodes()
    if len(sel) != 2:
        raise ReframeBakeError(
            "Select exactly 2 nodes:\n"
            "  - one Transform\n"
            "  - one Camera"
        )

    transforms = [n for n in sel if n.Class() == "Transform"]
    cameras = [n for n in sel if n.Class().startswith("Camera")]

    if len(transforms) != 1 or len(cameras) != 1:
        raise ReframeBakeError(
            "Selection must contain exactly:\n"
            "  - one Transform node\n"
            "  - one Camera/Camera2-style node"
        )

    return transforms[0], cameras[0]


def _knob_xy(knob):
    try:
        v = knob.value()
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            return float(v[0]), float(v[1])
    except Exception:
        pass

    try:
        return float(knob.getValue(0)), float(knob.getValue(1))
    except Exception:
        pass

    raise ReframeBakeError("Could not read x/y values from knob '{}'".format(knob.name()))


def _is_animated(knob):
    try:
        return bool(knob.isAnimated())
    except Exception:
        return False


def _uniform_scale(scale_knob):
    sx, sy = _knob_xy(scale_knob)
    if abs(sx - sy) > 1e-6:
        raise ReframeBakeError(
            "Transform scale must be uniform.\n"
            "Found scale.x = {:.10f}, scale.y = {:.10f}".format(sx, sy)
        )
    return float(sx)


def _validate_transform(node):
    for k in ("translate", "scale", "center"):
        if k not in node.knobs():
            raise ReframeBakeError("Transform is missing knob '{}'".format(k))
        if _is_animated(node[k]):
            raise ReframeBakeError(
                "This script only supports static Transform values.\n"
                "Knob '{}' is animated.".format(k)
            )

    if "rotate" in node.knobs():
        rot = float(node["rotate"].value())
        if abs(rot) > EPS:
            raise ReframeBakeError("Transform rotate must be 0.")

    for k in ("skewX", "skewY"):
        if k in node.knobs() and abs(float(node[k].value())) > EPS:
            raise ReframeBakeError("{} must be 0.".format(k))


def _camera_is_reading_from_file(node):
    if "read_from_file" not in node.knobs():
        return False
    try:
        return bool(node["read_from_file"].value())
    except Exception:
        return False


def _evaluate_file_knob(node, knob_name):
    if knob_name not in node.knobs():
        raise ReframeBakeError("Node '{}' has no '{}' knob".format(node.name(), knob_name))

    knob = node[knob_name]
    try:
        path = knob.evaluate()
    except Exception:
        path = knob.value()
    return path or ""


def _is_ascii_fbx(path):
    with open(path, "rb") as fh:
        header = fh.read(64)
    return b"Kaydara FBX Binary" not in header


def _validate_camera_common(node):
    for k in ("focal", "haperture", "vaperture"):
        if k not in node.knobs():
            raise ReframeBakeError(
                "Camera node '{}' is missing required knob '{}'".format(node.name(), k)
            )

    if "win_translate" in node.knobs():
        wx, wy = _knob_xy(node["win_translate"])
        if abs(wx) > EPS or abs(wy) > EPS:
            raise ReframeBakeError(
                "Camera win_translate must be 0,0.\n"
                "Found ({:.10f}, {:.10f})".format(wx, wy)
            )

    if "win_scale" in node.knobs():
        sx, sy = _knob_xy(node["win_scale"])
        if abs(sx - 1.0) > EPS or abs(sy - 1.0) > EPS:
            raise ReframeBakeError(
                "Camera win_scale must be 1,1.\n"
                "Found ({:.10f}, {:.10f})".format(sx, sy)
            )

    if "winroll" in node.knobs():
        wr = float(node["winroll"].value())
        if abs(wr) > EPS:
            raise ReframeBakeError("Camera winroll must be 0.")


def _validate_existing_fbx_camera(node):
    _validate_camera_common(node)

    if not _camera_is_reading_from_file(node):
        raise ReframeBakeError("Camera is not set to 'read from file'.")

    if "file" not in node.knobs():
        raise ReframeBakeError("Camera node has no 'file' knob.")

    path = _evaluate_file_knob(node, "file")
    if not path:
        raise ReframeBakeError("Camera file path is empty.")
    if not path.lower().endswith(".fbx"):
        raise ReframeBakeError("Camera file must be an .fbx:\n{}".format(path))
    if not os.path.exists(path):
        raise ReframeBakeError("FBX file does not exist:\n{}".format(path))
    if not _is_ascii_fbx(path):
        raise ReframeBakeError("FBX is binary. This script edits ASCII FBX only:\n{}".format(path))
    return path


def _camera_is_static_in_nuke(camera):
    """
    Conservative static check for a native Nuke camera.
    If any common transform / lens knob is animated, treat it as animated.
    """
    knobs_to_check = [
        "translate", "rotate", "scaling", "uniform_scale", "skew", "pivot",
        "focal", "haperture", "vaperture", "near", "far",
        "win_translate", "win_scale", "winroll"
    ]
    for k in knobs_to_check:
        if k in camera.knobs():
            if _is_animated(camera[k]):
                return False
    return True


def _read_text(path):
    with open(path, "r", encoding="utf-8", errors="ignore", newline="") as fh:
        return fh.read()


def _write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _find_matching_brace(text, open_brace_idx):
    """
    Brace matcher that ignores braces inside quoted strings.
    """
    depth = 0
    in_string = False
    escaped = False

    for i in range(open_brace_idx, len(text)):
        ch = text[i]

        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
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

    raise ReframeBakeError("Could not find matching brace in FBX text.")


def _base_name(name):
    s = name.split(":")[-1]
    s = s.split("|")[-1]
    return s.strip()


def _fbx_objects(text):
    objs = {
        "models": [],
        "camera_attrs": [],
        "curve_nodes": [],
        "curves": [],
    }

    specs = [
        ("models", re.compile(
            r'Model:\s*(\d+)\s*,\s*"Model::([^"]+)"\s*,\s*"Camera"\s*\{',
            re.MULTILINE
        )),
        ("camera_attrs", re.compile(
            r'NodeAttribute:\s*(\d+)\s*,\s*"NodeAttribute::([^"]+)"\s*,\s*"Camera"\s*\{',
            re.MULTILINE
        )),
        ("curve_nodes", re.compile(
            r'AnimationCurveNode:\s*(\d+)\s*,\s*"([^"]+)"\s*,\s*""\s*\{',
            re.MULTILINE
        )),
        ("curves", re.compile(
            r'AnimationCurve:\s*(\d+)\s*,\s*"([^"]*)"\s*,\s*""\s*\{',
            re.MULTILINE
        )),
    ]

    for key, pat in specs:
        for m in pat.finditer(text):
            obj_id = int(m.group(1))
            name = m.group(2)
            open_brace = text.find("{", m.start())
            close_brace = _find_matching_brace(text, open_brace)
            objs[key].append({
                "id": obj_id,
                "name": name,
                "start": m.start(),
                "end": close_brace + 1,
                "text": text[m.start():close_brace + 1],
            })

    return objs


def _fbx_connections(text):
    oo = []
    op = []

    oo_pat = re.compile(r'C:\s*"OO"\s*,\s*(-?\d+)\s*,\s*(-?\d+)', re.MULTILINE)
    op_pat = re.compile(
        r'C:\s*"OP"\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*"([^"]+)"',
        re.MULTILINE
    )

    for m in oo_pat.finditer(text):
        oo.append((int(m.group(1)), int(m.group(2))))

    for m in op_pat.finditer(text):
        op.append((int(m.group(1)), int(m.group(2)), m.group(3)))

    return oo, op


def _choose_by_name(items, target_name, kind_label):
    if not items:
        raise ReframeBakeError("No {} objects found.".format(kind_label))

    exact = [x for x in items if x["name"] == target_name]
    if len(exact) == 1:
        return exact[0]

    base = _base_name(target_name)
    loose = [x for x in items if _base_name(x["name"]) == base]
    if len(loose) == 1:
        return loose[0]

    names = "\n".join("  - {}".format(x["name"]) for x in items)
    raise ReframeBakeError(
        "Could not uniquely resolve {} '{}'. Available:\n{}".format(
            kind_label, target_name, names
        )
    )


def _resolve_camera_attribute_for_model(fbx_text, target_model_name):
    objs = _fbx_objects(fbx_text)
    oo_conns, _ = _fbx_connections(fbx_text)

    model = _choose_by_name(objs["models"], target_model_name, "camera model")
    attr_by_id = {x["id"]: x for x in objs["camera_attrs"]}

    matched_attrs = []
    for a, b in oo_conns:
        if a in attr_by_id and b == model["id"]:
            matched_attrs.append(attr_by_id[a])
        elif b in attr_by_id and a == model["id"]:
            matched_attrs.append(attr_by_id[b])

    matched_attrs = list({x["id"]: x for x in matched_attrs}.values())

    if len(matched_attrs) != 1:
        names = [x["name"] for x in matched_attrs]
        raise ReframeBakeError(
            "Could not uniquely resolve the camera attribute connected to model '{}'. "
            "Matched attributes: {}".format(target_model_name, names)
        )

    return model, matched_attrs[0]


def _get_property(block_text, prop_name, default=None):
    pattern = re.compile(
        r'^[ \t]*P:\s*"' + re.escape(prop_name) + r'"\s*,.*?,.*?,.*?,\s*([-+0-9.eE]+)',
        re.MULTILINE
    )
    m = pattern.search(block_text)
    if not m:
        return default
    return float(m.group(1))


def _set_existing_property_only(block_text, prop_name, value):
    value_str = "{:.15g}".format(float(value))
    pat = re.compile(
        r'(^[ \t]*P:\s*"' + re.escape(prop_name) + r'"\s*,.*?,.*?,.*?,\s*)([-+0-9.eE]+)([^\n]*$)',
        re.MULTILINE
    )
    if pat.search(block_text):
        return pat.sub(r"\1" + value_str + r"\3", block_text, count=1)
    return block_text


def _set_anim_curve_values(curve_block_text, new_value):
    value_str = "{:.15g}".format(float(new_value))

    curve_block_text = re.sub(
        r'(^[ \t]*Default:\s*)([-+0-9.eE]+)',
        r"\1" + value_str,
        curve_block_text,
        count=1,
        flags=re.MULTILINE
    )

    def _replace_array(match):
        count = int(match.group(2))
        values = ",".join([value_str] * count)
        return "{}{}{}{}{}".format(
            match.group(1), count, match.group(3), values, match.group(5)
        )

    curve_block_text = re.sub(
        r'(KeyValueFloat:\s*\*)(\d+)(\s*\{\s*a:\s*)([^}]*)((\s*)\})',
        _replace_array,
        curve_block_text,
        count=1,
        flags=re.MULTILINE | re.DOTALL
    )

    curve_block_text = re.sub(
        r'(KeyValueDouble:\s*\*)(\d+)(\s*\{\s*a:\s*)([^}]*)((\s*)\})',
        _replace_array,
        curve_block_text,
        count=1,
        flags=re.MULTILINE | re.DOTALL
    )

    return curve_block_text


def _get_effective_property_value(fbx_text, target_model_name, property_name, fallback=0.0):
    """
    Reads the current effective value in order of precedence:
      1) connected AnimationCurve Default
      2) connected AnimationCurveNode d|prop
      3) NodeAttribute prop
      4) fallback
    """
    model_obj, attr_obj = _resolve_camera_attribute_for_model(fbx_text, target_model_name)
    objs = _fbx_objects(fbx_text)
    _, op_conns = _fbx_connections(fbx_text)

    curve_nodes_by_id = {x["id"]: x for x in objs["curve_nodes"]}
    curves_by_id = {x["id"]: x for x in objs["curves"]}

    curve_node_obj = None
    for src_id, dst_id, prop_name in op_conns:
        if dst_id == attr_obj["id"] and prop_name == property_name and src_id in curve_nodes_by_id:
            curve_node_obj = curve_nodes_by_id[src_id]
            break

    if curve_node_obj is not None:
        curve_obj = None
        wanted = "d|" + property_name
        for src_id, dst_id, prop_name in op_conns:
            if dst_id == curve_node_obj["id"] and prop_name == wanted and src_id in curves_by_id:
                curve_obj = curves_by_id[src_id]
                break

        if curve_obj is not None:
            m = re.search(r'^[ \t]*Default:\s*([-+0-9.eE]+)', curve_obj["text"], re.MULTILINE)
            if m:
                return float(m.group(1))

        val = _get_property(curve_node_obj["text"], "d|" + property_name, None)
        if val is not None:
            return val

    val = _get_property(attr_obj["text"], property_name, None)
    if val is not None:
        return val

    return fallback


def _patch_one_camera_property(full_text, target_model_name, property_name, new_value):
    """
    Patches:
      - NodeAttribute prop, if present
      - connected AnimationCurveNode d|prop, if present
      - connected AnimationCurve Default and KeyValue arrays, if present
    """
    model_obj, attr_obj = _resolve_camera_attribute_for_model(full_text, target_model_name)
    objs = _fbx_objects(full_text)
    _, op_conns = _fbx_connections(full_text)

    curve_nodes_by_id = {x["id"]: x for x in objs["curve_nodes"]}
    curves_by_id = {x["id"]: x for x in objs["curves"]}

    replacements = []

    attr_text_new = _set_existing_property_only(attr_obj["text"], property_name, new_value)
    if attr_text_new != attr_obj["text"]:
        replacements.append((attr_obj["start"], attr_obj["end"], attr_text_new))

    curve_node_obj = None
    for src_id, dst_id, prop_name in op_conns:
        if dst_id == attr_obj["id"] and prop_name == property_name and src_id in curve_nodes_by_id:
            curve_node_obj = curve_nodes_by_id[src_id]
            break

    if curve_node_obj is not None:
        cn_text_new = _set_existing_property_only(curve_node_obj["text"], "d|" + property_name, new_value)
        if cn_text_new != curve_node_obj["text"]:
            replacements.append((curve_node_obj["start"], curve_node_obj["end"], cn_text_new))

        curve_obj = None
        wanted = "d|" + property_name
        for src_id, dst_id, prop_name in op_conns:
            if dst_id == curve_node_obj["id"] and prop_name == wanted and src_id in curves_by_id:
                curve_obj = curves_by_id[src_id]
                break

        if curve_obj is not None:
            c_text_new = _set_anim_curve_values(curve_obj["text"], new_value)
            if c_text_new != curve_obj["text"]:
                replacements.append((curve_obj["start"], curve_obj["end"], c_text_new))

    for start, end, repl in sorted(replacements, key=lambda x: x[0], reverse=True):
        full_text = full_text[:start] + repl + full_text[end:]

    return full_text


def _unique_output_path(src_path, suffix="_nukeReframe"):
    base, ext = os.path.splitext(src_path)
    candidate = base + suffix + ext
    if not os.path.exists(candidate):
        return candidate

    i = 1
    while True:
        candidate = "{}{}_{:02d}{}".format(base, suffix, i, ext)
        if not os.path.exists(candidate):
            return candidate
        i += 1


def _set_choice_knob_exact(node, knob_name, wanted):
    if knob_name not in node.knobs():
        return False

    knob = node[knob_name]

    try:
        vals = list(knob.values())
        if wanted in vals:
            knob.setValue(vals.index(wanted))
            return True
    except Exception:
        pass

    try:
        knob.setValue(wanted)
        return True
    except Exception:
        return False


def _reload_camera_from_file(camera_node, file_path, target_name=None):
    if "read_from_file" in camera_node.knobs():
        camera_node["read_from_file"].setValue(True)

    if "file" not in camera_node.knobs():
        raise ReframeBakeError("Camera node '{}' has no file knob.".format(camera_node.name()))

    camera_node["file"].setValue(file_path)

    if "reload" in camera_node.knobs():
        try:
            camera_node["reload"].execute()
        except Exception:
            try:
                nuke.executeInMainThread(camera_node["reload"].execute)
            except Exception:
                pass

    if target_name and "fbx_node_name" in camera_node.knobs():
        _set_choice_knob_exact(camera_node, "fbx_node_name", target_name)
        if "reload" in camera_node.knobs():
            try:
                camera_node["reload"].execute()
            except Exception:
                try:
                    nuke.executeInMainThread(camera_node["reload"].execute)
                except Exception:
                    pass


def _create_imported_camera(file_path, target_name=None, ref_node=None):
    try:
        cam = nuke.nodes.Camera2()
    except Exception:
        cam = nuke.nodes.Camera()

    if ref_node is not None:
        try:
            cam.setXpos(ref_node.xpos() + 140)
            cam.setYpos(ref_node.ypos())
        except Exception:
            pass

    try:
        cam.setName((ref_node.name() if ref_node else "Camera") + "_FBX")
    except Exception:
        pass

    _reload_camera_from_file(cam, file_path, target_name=target_name)
    return cam


def _ask_save_fbx_path(camera_node):
    script_path = nuke.root().name()
    if script_path and script_path != "Root":
        base_dir = os.path.dirname(script_path)
    else:
        base_dir = os.path.expanduser("~")

    default_name = camera_node.name() + "_nukeReframe_clean.fbx"
    default_path = os.path.join(base_dir, default_name)

    path = nuke.getFilename("Save ASCII FBX camera as", "*.fbx", default_path)
    if not path:
        return ""

    if not path.lower().endswith(".fbx"):
        path += ".fbx"

    return path


def _export_camera_to_new_ascii_fbx(camera_node, out_path, single_frame=False):
    temp_scene = None
    temp_writegeo = None
    try:
        temp_scene = nuke.nodes.Scene(inputs=[camera_node])
        temp_writegeo = nuke.nodes.WriteGeo(inputs=[temp_scene])

        temp_writegeo["file"].setValue(out_path)

        if "file_type" in temp_writegeo.knobs():
            try:
                temp_writegeo["file_type"].setValue("fbx")
            except Exception:
                try:
                    temp_writegeo["file_type"].setValue(1)
                except Exception:
                    pass

        if "writeCameras" in temp_writegeo.knobs():
            temp_writegeo["writeCameras"].setValue(True)
        if "writeGeometries" in temp_writegeo.knobs():
            temp_writegeo["writeGeometries"].setValue(False)
        if "writeLights" in temp_writegeo.knobs():
            temp_writegeo["writeLights"].setValue(False)
        if "writeAxes" in temp_writegeo.knobs():
            temp_writegeo["writeAxes"].setValue(False)
        if "writePointClouds" in temp_writegeo.knobs():
            temp_writegeo["writePointClouds"].setValue(False)
        if "asciiFileFormat" in temp_writegeo.knobs():
            temp_writegeo["asciiFileFormat"].setValue(True)

        if single_frame:
            frame = int(nuke.frame())
            nuke.execute(temp_writegeo.name(), frame, frame)
        else:
            first = int(nuke.root()["first_frame"].value())
            last = int(nuke.root()["last_frame"].value())
            nuke.execute(temp_writegeo.name(), first, last)

    finally:
        for node in (temp_writegeo, temp_scene):
            if node is not None:
                try:
                    nuke.delete(node)
                except Exception:
                    pass


def _compute_reframe_values(transform, camera):
    tx, ty = _knob_xy(transform["translate"])
    cx, cy = _knob_xy(transform["center"])
    scale = _uniform_scale(transform["scale"])

    fmt = transform.input(0).format() if transform.input(0) else nuke.root().format()
    frame_w = float(fmt.width())
    frame_h = float(fmt.height())

    if frame_w <= 0 or frame_h <= 0:
        raise ReframeBakeError("Invalid frame format size.")

    sensor_w_mm = float(camera["haperture"].value())
    sensor_h_mm = float(camera["vaperture"].value())
    focal_mm = float(camera["focal"].value())

    shift_x_px = tx + (1.0 - scale) * (cx - frame_w / 2.0)
    shift_y_px = ty + (1.0 - scale) * (cy - frame_h / 2.0)

    delta_hfo_in = (shift_x_px / frame_w) * (sensor_w_mm * MM_TO_INCH)
    delta_vfo_in = (shift_y_px / frame_h) * (sensor_h_mm * MM_TO_INCH)
    new_focal_mm = scale * focal_mm

    return {
        "tx": tx, "ty": ty,
        "cx": cx, "cy": cy,
        "scale": scale,
        "frame_w": frame_w, "frame_h": frame_h,
        "sensor_w_mm": sensor_w_mm, "sensor_h_mm": sensor_h_mm,
        "focal_mm": focal_mm,
        "delta_hfo_in": delta_hfo_in,
        "delta_vfo_in": delta_vfo_in,
        "new_focal_mm": new_focal_mm,
    }


def _patch_fbx_camera_for_model(src_fbx, target_model_name,
                                delta_hfo_in, delta_vfo_in, new_focal_mm,
                                out_fbx=None):
    fbx_text = _read_text(src_fbx)

    # Read current effective offsets from the file, not just the NodeAttribute,
    # so that existing keyed/default values are composed correctly.
    old_hfo = _get_effective_property_value(fbx_text, target_model_name, "FilmOffsetX", fallback=0.0)
    old_vfo = _get_effective_property_value(fbx_text, target_model_name, "FilmOffsetY", fallback=0.0)

    final_hfo = old_hfo + delta_hfo_in
    final_vfo = old_vfo + delta_vfo_in

    fbx_text = _patch_one_camera_property(fbx_text, target_model_name, "FocalLength", new_focal_mm)
    fbx_text = _patch_one_camera_property(fbx_text, target_model_name, "FilmOffsetX", final_hfo)
    fbx_text = _patch_one_camera_property(fbx_text, target_model_name, "FilmOffsetY", final_vfo)

    model_obj, attr_obj = _resolve_camera_attribute_for_model(fbx_text, target_model_name)

    if out_fbx is None:
        out_fbx = _unique_output_path(src_fbx, suffix="_nukeReframe")

    _write_text(out_fbx, fbx_text)

    return {
        "out_fbx": out_fbx,
        "target_model_name": model_obj["name"],
        "target_attr_name": attr_obj["name"],
        "final_hfo": final_hfo,
        "final_vfo": final_vfo,
        "new_focal_mm": new_focal_mm,
    }


def _report_text(mode_label, transform, camera, comp, path_hint, target_name, extra=""):
    lines = [
        "Dry run summary",
        "---------------",
        "Mode:           {}".format(mode_label),
        "Transform node: {}".format(transform.name()),
        "Camera node:    {}".format(camera.name()),
        "Target camera:  {}".format(repr(target_name)),
        "FBX path:       {}".format(path_hint),
        "",
        "Frame size:     {} x {}".format(int(comp["frame_w"]), int(comp["frame_h"])),
        "Translate:      ({:.10f}, {:.10f}) px".format(comp["tx"], comp["ty"]),
        "Center:         ({:.10f}, {:.10f}) px".format(comp["cx"], comp["cy"]),
        "Scale:          {:.10f}".format(comp["scale"]),
        "",
        "Sensor:         {:.10f} mm x {:.10f} mm".format(comp["sensor_w_mm"], comp["sensor_h_mm"]),
        "Old focal:      {:.10f} mm".format(comp["focal_mm"]),
        "New focal:      {:.10f} mm".format(comp["new_focal_mm"]),
        "",
        "Delta HFO:      {:.12f} in".format(comp["delta_hfo_in"]),
        "Delta VFO:      {:.12f} in".format(comp["delta_vfo_in"]),
    ]
    if extra:
        lines.extend(["", extra])
    return "\n".join(lines)


def bake_selected_transform_into_camera_or_fbx():
    transform, camera = _selected_transform_and_camera()
    _validate_transform(transform)
    _validate_camera_common(camera)

    comp = _compute_reframe_values(transform, camera)

    if _camera_is_reading_from_file(camera):
        src_fbx = _validate_existing_fbx_camera(camera)

        target_model_name = ""
        if "fbx_node_name" in camera.knobs():
            try:
                target_model_name = str(camera["fbx_node_name"].value()).strip()
            except Exception:
                target_model_name = ""
        if not target_model_name:
            target_model_name = camera.name()

        report = _report_text(
            "Patch existing ASCII FBX",
            transform, camera, comp, src_fbx, target_model_name
        )
        print(report)

        if not nuke.ask(report + "\n\nWrite a new patched FBX?"):
            return

        patch_info = _patch_fbx_camera_for_model(
            src_fbx=src_fbx,
            target_model_name=target_model_name,
            delta_hfo_in=comp["delta_hfo_in"],
            delta_vfo_in=comp["delta_vfo_in"],
            new_focal_mm=comp["new_focal_mm"],
            out_fbx=None,
        )

        msg = (
            "Wrote new FBX:\n{}\n\n"
            "Patched camera model: {}\n"
            "Connected camera attribute: {}\n\n"
            "Do you want to repoint the selected Camera to this new file and reload it now?"
        ).format(
            patch_info["out_fbx"],
            patch_info["target_model_name"],
            patch_info["target_attr_name"],
        )

        if nuke.ask(msg):
            _reload_camera_from_file(
                camera,
                patch_info["out_fbx"],
                target_name=patch_info["target_model_name"]
            )
            _msg(
                "Done.\n\n"
                "Camera repointed and reload triggered:\n{}\n\n"
                "fbx_node_name set to: {}".format(
                    patch_info["out_fbx"],
                    patch_info["target_model_name"]
                )
            )
        else:
            _msg("Done.\n\nNew FBX written:\n{}".format(patch_info["out_fbx"]))

    else:
        out_fbx = _ask_save_fbx_path(camera)
        if not out_fbx:
            return

        if os.path.exists(out_fbx):
            if not nuke.ask("File already exists:\n{}\n\nOverwrite it?".format(out_fbx)):
                return

        is_static = _camera_is_static_in_nuke(camera)
        extra = "Export mode: {} frame{}".format(
            "single" if is_static else "full range",
            "" if is_static else "s"
        )

        report = _report_text(
            "Create brand-new ASCII FBX",
            transform, camera, comp, out_fbx, camera.name(), extra=extra
        )
        print(report)

        if not nuke.ask(report + "\n\nExport a new ASCII FBX, patch it, and import it back into Nuke?"):
            return

        _export_camera_to_new_ascii_fbx(camera, out_fbx, single_frame=is_static)

        if not os.path.exists(out_fbx):
            raise ReframeBakeError(
                "WriteGeo did not produce the expected FBX file:\n{}".format(out_fbx)
            )

        if not _is_ascii_fbx(out_fbx):
            raise ReframeBakeError(
                "The exported FBX is not ASCII:\n{}".format(out_fbx)
            )

        patch_info = _patch_fbx_camera_for_model(
            src_fbx=out_fbx,
            target_model_name=camera.name(),
            delta_hfo_in=comp["delta_hfo_in"],
            delta_vfo_in=comp["delta_vfo_in"],
            new_focal_mm=comp["new_focal_mm"],
            out_fbx=out_fbx,
        )

        new_cam = _create_imported_camera(
            file_path=out_fbx,
            target_name=patch_info["target_model_name"],
            ref_node=camera
        )

        _msg(
            "Done.\n\n"
            "Created and imported new ASCII FBX camera:\n{}\n\n"
            "Imported camera node: {}\n"
            "fbx_node_name set to: {}\n"
            "Patched camera attribute: {}\n\n"
            .format(
                out_fbx,
                new_cam.name(),
                patch_info["target_model_name"],
                patch_info["target_attr_name"],
            )
        )


try:
    bake_selected_transform_into_camera_or_fbx()
except ReframeBakeError as e:
    _msg("Reframe bake failed:\n\n{}".format(e))
    raise
except Exception as e:
    _msg("Unexpected error:\n\n{}".format(e))
    raise