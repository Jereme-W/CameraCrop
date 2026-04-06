
import os
import re

import nuke
from PySide2 import QtCore

MM_TO_INCH = 0.0393701
EPS = 1e-8
FBX_TICKS_PER_SECOND = 46186158000


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


def _knob_value(knob, frame=None, index=None):
    try:
        if frame is None:
            if index is None:
                return float(knob.value())
            return float(knob.getValue(index))
        if index is None:
            try:
                return float(knob.valueAt(frame))
            except TypeError:
                return float(knob.valueAt(frame, 0))
        return float(knob.valueAt(frame, index))
    except Exception:
        where = " at frame {}".format(frame) if frame is not None else ""
        if index is None:
            raise ReframeBakeError("Failed to read knob '{}'{}.".format(knob.name(), where))
        raise ReframeBakeError(
            "Failed to read knob '{}' channel {}{}.".format(knob.name(), index, where)
        )


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


def _is_animated(knob, channel=None):
    try:
        if channel is None:
            return bool(knob.isAnimated())
        return bool(knob.isAnimated(channel))
    except Exception:
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


def _to_nuke_path(path):
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
    chosen = nuke.getFilename(
        "Choose output folder",
        "*",
        os.path.join(default_dir, "select_folder"),
    )
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
        "models": re.compile(
            r'Model:\s*(\d+)\s*,\s*"Model::([^"]+)"\s*,\s*"Camera"\s*\{',
            re.MULTILINE,
        ),
        "attrs": re.compile(
            r'NodeAttribute:\s*(\d+)\s*,\s*"NodeAttribute::([^"]+)"\s*,\s*"Camera"\s*\{',
            re.MULTILINE,
        ),
        "curve_nodes": re.compile(
            r'AnimationCurveNode:\s*(\d+)\s*,\s*"([^"]+)"\s*,\s*""\s*\{',
            re.MULTILINE,
        ),
        "curves": re.compile(
            r'AnimationCurve:\s*(\d+)\s*,\s*"([^"]*)"\s*,\s*""\s*\{',
            re.MULTILINE,
        ),
    }
    out = {k: [] for k in specs}
    for key, pat in specs.items():
        for m in pat.finditer(text):
            open_idx = text.find("{", m.start())
            close_idx = _find_matching_brace(text, open_idx)
            out[key].append(
                {
                    "id": int(m.group(1)),
                    "name": m.group(2),
                    "start": m.start(),
                    "end": close_idx + 1,
                    "text": text[m.start() : close_idx + 1],
                }
            )
    return out


def _fbx_connections(text):
    oo = []
    op = []
    for m in re.finditer(r'C:\s*"OO"\s*,\s*(-?\d+)\s*,\s*(-?\d+)', text, re.MULTILINE):
        oo.append((int(m.group(1)), int(m.group(2))))
    for m in re.finditer(
        r'C:\s*"OP"\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*"([^"]+)"',
        text,
        re.MULTILINE,
    ):
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


def _linked_camera_attrs_for_model(model_id, attrs_by_id, oo):
    matched = []
    for a, b in oo:
        if a in attrs_by_id and b == model_id:
            matched.append(attrs_by_id[a])
        elif b in attrs_by_id and a == model_id:
            matched.append(attrs_by_id[b])
    return list({x["id"]: x for x in matched}.values())



def _score_camera_model_candidate(full_text, model, attrs_by_id, oo, target_model_name):
    score = 0
    base_target = _base_name(target_model_name)
    model_name = model["name"]
    model_base = _base_name(model_name)

    if model_name == target_model_name:
        score += 100
    if model_base == base_target:
        score += 50
    if model_name not in PRODUCER_NAMES and model_base not in PRODUCER_NAMES:
        score += 20

    linked_attrs = _linked_camera_attrs_for_model(model["id"], attrs_by_id, oo)
    if len(linked_attrs) == 1:
        score += 20
        attr = linked_attrs[0]
        if _get_prop(attr["text"], "FocalLength", None) is not None:
            score += 10
        for prop_name in ("FocalLength", "FilmOffsetX", "FilmOffsetY"):
            curve_node_id, curve_id = _property_curve_links_for_object(full_text, attr["id"], prop_name)
            if curve_node_id is not None:
                score += 2
            if curve_id is not None:
                score += 3
    else:
        score -= 100

    return score, linked_attrs



def _resolve_camera_blocks(text, target_model_name):
    objs = _fbx_objects(text)
    oo, _ = _fbx_connections(text)

    if not objs["models"]:
        raise ReframeBakeError("No FBX camera Model objects found.")
    if not objs["attrs"]:
        raise ReframeBakeError("No FBX camera NodeAttribute objects found.")

    attrs_by_id = {x["id"]: x for x in objs["attrs"]}

    exact = [x for x in objs["models"] if x["name"] == target_model_name]
    base = _base_name(target_model_name)
    loose = [x for x in objs["models"] if _base_name(x["name"]) == base]

    if exact:
        candidates = exact
    elif loose:
        candidates = loose
    else:
        raise ReframeBakeError("Could not resolve camera model '{}'".format(target_model_name))

    if len(candidates) == 1:
        model = candidates[0]
        matched = _linked_camera_attrs_for_model(model["id"], attrs_by_id, oo)
        if len(matched) != 1:
            raise ReframeBakeError(
                "Could not uniquely find camera attribute linked to model '{}'".format(
                    model["name"]
                )
            )
        return objs, model, matched[0]

    ranked = []
    for model in candidates:
        score, linked_attrs = _score_camera_model_candidate(
            text,
            model,
            attrs_by_id,
            oo,
            target_model_name,
        )
        ranked.append((score, model["start"], model, linked_attrs))

    ranked.sort(key=lambda x: (x[0], x[1]))
    _score, _start, model, matched = ranked[-1]
    if len(matched) != 1:
        raise ReframeBakeError(
            "Could not uniquely find camera attribute linked to model '{}'".format(
                model["name"]
            )
        )
    return objs, model, matched[0]


def _get_prop(text, name, default=None):
    m = re.search(
        r'^[ \t]*P:\s*"{}"\s*,.*?,.*?,.*?,\s*([-+0-9.eE]+)'.format(re.escape(name)),
        text,
        re.MULTILINE,
    )
    return float(m.group(1)) if m else default


def _set_existing_prop(text, name, value):
    val = "{:.15g}".format(float(value))
    pat = re.compile(
        r'(^[ \t]*P:\s*"' + re.escape(name) + r'"\s*,.*?,.*?,.*?,\s*)([-+0-9.eE]+)([^\n]*$)',
        re.MULTILINE,
    )
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

    props_body = block_text[m.end() :]
    p = re.search(r'^[ \t]*P:\s*"', props_body, re.MULTILINE)
    if p:
        indent = re.match(r"[ \t]*", props_body[p.start() :]).group(0)
    else:
        indent = " " * 12

    val = "{:.15g}".format(float(value))
    line = '{}P: "{}", "Number", "", "A",{}\n'.format(indent, name, val)
    return block_text[: m.end()] + line + block_text[m.end() :]


def _parse_float_array_block(array_text):
    tokens = [v.strip() for v in array_text.split(",") if v.strip()]
    if not tokens:
        return []
    out = []
    for v in tokens:
        try:
            out.append(float(v))
        except Exception:
            return None
    return out


def _replace_array_values(block_text, key_name, xform):
    pat = re.compile(
        r"(" + re.escape(key_name) + r':\s*\*)(\d+)(\s*\{\s*a:\s*)([^}]*)((\s*)\})',
        re.MULTILINE | re.DOTALL,
    )
    m = pat.search(block_text)
    if not m:
        return block_text
    vals = _parse_float_array_block(m.group(4))
    if vals is None:
        return block_text
    new_vals = ["{:.15g}".format(xform(v)) for v in vals]
    return pat.sub(r"\g<1>{}\g<3>{}\g<5>".format(m.group(2), ",".join(new_vals)), block_text, count=1)


def _patch_curve_block(curve_text, op):
    m = re.search(r"^[ \t]*Default:\s*([-+0-9.eE]+)", curve_text, re.MULTILINE)
    if m:
        old = float(m.group(1))
        new = op(old)
        curve_text = re.sub(
            r"(^[ \t]*Default:\s*)([-+0-9.eE]+)",
            lambda mm: "{}{}".format(mm.group(1), "{:.15g}".format(new)),
            curve_text,
            count=1,
            flags=re.MULTILINE,
        )
    curve_text = _replace_array_values(curve_text, "KeyValueFloat", op)
    curve_text = _replace_array_values(curve_text, "KeyValueDouble", op)
    return curve_text


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
    curve_node_id, curve_id = _property_curve_links_for_object(
        full_text, attr["id"], property_name
    )
    return attr["id"], curve_node_id, curve_id


def _camera_property_has_animation(full_text, target_model_name, property_name):
    _, curve_node_id, curve_id = _camera_property_curve_links(
        full_text, target_model_name, property_name
    )
    return curve_node_id is not None or curve_id is not None


def _remove_property_animation_links_for_object(full_text, object_id, property_name):
    curve_node_id, curve_id = _property_curve_links_for_object(full_text, object_id, property_name)
    if curve_node_id is None:
        return full_text

    pat_obj = re.compile(
        r'^\s*C:\s*"OP"\s*,\s*{}\s*,\s*{}\s*,\s*"{}"\s*\r?\n?'.format(
            curve_node_id, object_id, re.escape(property_name)
        ),
        re.MULTILINE,
    )
    full_text = pat_obj.sub("", full_text)

    if curve_id is not None:
        pat_curve = re.compile(
            r'^\s*C:\s*"OP"\s*,\s*{}\s*,\s*{}\s*,\s*"{}"\s*\r?\n?'.format(
                curve_id, curve_node_id, re.escape("d|" + property_name)
            ),
            re.MULTILINE,
        )
        full_text = pat_curve.sub("", full_text)

    return full_text


def _remove_camera_property_animation_links(full_text, target_model_name, property_name):
    attr_id, _, _ = _camera_property_curve_links(full_text, target_model_name, property_name)
    return _remove_property_animation_links_for_object(full_text, attr_id, property_name)


def _remove_static_camera_animation(full_text, target_model_name):
    _, model, attr = _resolve_camera_blocks(full_text, target_model_name)

    for prop in ("Lcl Translation", "Lcl Rotation", "Lcl Scaling"):
        full_text = _remove_property_animation_links_for_object(full_text, model["id"], prop)

    for prop in (
        "FocalLength",
        "FilmOffsetX",
        "FilmOffsetY",
        "NearPlane",
        "FarPlane",
        "Roll",
        "FieldOfView",
        "FieldOfViewX",
        "FieldOfViewY",
    ):
        full_text = _remove_property_animation_links_for_object(full_text, attr["id"], prop)

    full_text = _remove_unreferenced_animation_objects(full_text)
    return full_text


def _remove_unreferenced_animation_objects(full_text):
    objs = _fbx_objects(full_text)
    oo, op = _fbx_connections(full_text)
    referenced_ids = set()
    for a, b in oo:
        referenced_ids.add(a)
        referenced_ids.add(b)
    for a, b, _ in op:
        referenced_ids.add(a)
        referenced_ids.add(b)

    removals = []
    for item in objs["curves"] + objs["curve_nodes"]:
        if item["id"] not in referenced_ids:
            removals.append((item["start"], item["end"]))

    for s, e in sorted(removals, key=lambda x: x[0], reverse=True):
        full_text = full_text[:s] + full_text[e:]
    return full_text


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
            replacements.append(
                (
                    curve_node["start"],
                    curve_node["end"],
                    _set_existing_prop(curve_node["text"], "d|" + property_name, op(old_d)),
                )
            )

        curve_obj = None
        want = "d|" + property_name
        for src_id, dst_id, prop_name in op_conns:
            if dst_id == curve_node["id"] and prop_name == want and src_id in curves:
                curve_obj = curves[src_id]
                break
        if curve_obj is not None:
            replacements.append(
                (
                    curve_obj["start"],
                    curve_obj["end"],
                    _patch_curve_block(curve_obj["text"], op),
                )
            )

    for s, e, r in sorted(replacements, key=lambda x: x[0], reverse=True):
        full_text = full_text[:s] + r + full_text[e:]
    return full_text


def _patch_property_static(full_text, target_model_name, property_name, op):
    _, _, attr = _resolve_camera_blocks(full_text, target_model_name)
    old_val = _get_prop(attr["text"], property_name, 0.0)
    new_val = op(old_val)

    attr_new = _set_or_add_numeric_prop(attr["text"], property_name, new_val)
    if attr_new != attr["text"]:
        full_text = full_text[: attr["start"]] + attr_new + full_text[attr["end"] :]

    full_text = _remove_camera_property_animation_links(full_text, target_model_name, property_name)
    return full_text


def _remove_camera_vector_film_offset(full_text, target_model_name):
    _, _, attr = _resolve_camera_blocks(full_text, target_model_name)
    attr_new = re.sub(
        r'^[ \t]*P:\s*"FilmOffset"\s*,\s*"Vector[^"]*"\s*,.*$\r?\n?',
        "",
        attr["text"],
        flags=re.MULTILINE,
    )
    if attr_new == attr["text"]:
        return full_text
    return full_text[: attr["start"]] + attr_new + full_text[attr["end"] :]


def _set_default_camera_in_fbx_text(source_text, camera_name):
    if not camera_name:
        return source_text

    pat = re.compile(
        r'(^[ \t]*P:\s*"DefaultCamera"\s*,\s*"KString"\s*,\s*""\s*,\s*""\s*,\s*")[^"]*(".*$)',
        re.MULTILINE,
    )
    if pat.search(source_text):
        safe_name = camera_name.replace("\\", "\\\\").replace('"', '\\"')
        return pat.sub(lambda m: "{}{}{}".format(m.group(1), safe_name, m.group(2)), source_text, count=1)

    block = re.search(
        r"(GlobalSettings:\s*\{\s*\r?\n(?:.*?\r?\n)*?[ \t]*Properties70:\s*\{\s*\r?\n)",
        source_text,
        re.DOTALL,
    )
    if not block:
        return source_text

    return (
        source_text[: block.end()]
        + '        P: "DefaultCamera", "KString", "", "", "{}"\n'.format(camera_name)
        + source_text[block.end() :]
    )


def _rename_exported_camera(source_text, from_camera_name, to_camera_name):
    if not from_camera_name or not to_camera_name or from_camera_name == to_camera_name:
        return source_text

    _, model, attr = _resolve_camera_blocks(source_text, from_camera_name)
    model_new = re.sub(
        r'(Model:\s*{}\s*,\s*"Model::)([^"]+)(")'.format(model["id"]),
        lambda m: "{}{}{}".format(m.group(1), to_camera_name, m.group(3)),
        model["text"],
        count=1,
    )
    attr_new = re.sub(
        r'(NodeAttribute:\s*{}\s*,\s*"NodeAttribute::)([^"]+)(")'.format(attr["id"]),
        lambda m: "{}{}{}".format(m.group(1), to_camera_name, m.group(3)),
        attr["text"],
        count=1,
    )

    out = source_text
    out = out[: model["start"]] + model_new + out[model["end"] :]
    shift = len(model_new) - (model["end"] - model["start"])
    attr_start = attr["start"] + shift if attr["start"] > model["start"] else attr["start"]
    attr_end = attr["end"] + shift if attr["end"] > model["start"] else attr["end"]
    out = out[:attr_start] + attr_new + out[attr_end:]
    return out


def _parse_array_block_with_count(block_text, key_name):
    pat = re.compile(
        r"(" + re.escape(key_name) + r':\s*\*)(\d+)(\s*\{\s*a:\s*)([^}]*)((\s*)\})',
        re.MULTILINE | re.DOTALL,
    )
    m = pat.search(block_text)
    if not m:
        return None
    raw_vals = [v.strip() for v in m.group(4).split(",") if v.strip()]
    return {
        "match": m,
        "count": int(m.group(2)),
        "raw_values": raw_vals,
    }


def _replace_array_block(block_text, key_name, formatted_values):
    pat = re.compile(
        r"(" + re.escape(key_name) + r':\s*\*)(\d+)(\s*\{\s*a:\s*)([^}]*)((\s*)\})',
        re.MULTILINE | re.DOTALL,
    )
    m = pat.search(block_text)
    if not m:
        raise ReframeBakeError("Could not find FBX array '{}'.".format(key_name))
    return pat.sub(
        r"\g<1>{}\g<3>{}\g<5>".format(len(formatted_values), ",".join(formatted_values)),
        block_text,
        count=1,
    )


def _frame_to_fbx_time(frame, fps):
    return int(round((float(frame) / float(fps)) * FBX_TICKS_PER_SECOND))


def _set_curve_block_sampled_values(curve_text, frames, values, fps):
    time_info = _parse_array_block_with_count(curve_text, "KeyTime")
    if time_info is None:
        raise ReframeBakeError("Animated FBX curve is missing KeyTime.")
    if time_info["count"] != len(frames):
        raise ReframeBakeError(
            "Animated FBX curve key count mismatch. Expected {}, found {}.".format(
                len(frames), time_info["count"]
            )
        )

    value_key_name = None
    value_info = _parse_array_block_with_count(curve_text, "KeyValueFloat")
    if value_info is not None:
        value_key_name = "KeyValueFloat"
    else:
        value_info = _parse_array_block_with_count(curve_text, "KeyValueDouble")
        if value_info is not None:
            value_key_name = "KeyValueDouble"

    if value_info is None:
        raise ReframeBakeError("Animated FBX curve is missing KeyValueFloat/KeyValueDouble.")
    if value_info["count"] != len(values):
        raise ReframeBakeError(
            "Animated FBX curve value count mismatch. Expected {}, found {}.".format(
                len(values), value_info["count"]
            )
        )

    formatted_times = [str(_frame_to_fbx_time(fr, fps)) for fr in frames]
    formatted_values = ["{:.15g}".format(float(v)) for v in values]

    curve_text = _replace_array_block(curve_text, "KeyTime", formatted_times)
    curve_text = _replace_array_block(curve_text, value_key_name, formatted_values)

    if re.search(r"^[ \t]*Default:\s*([-+0-9.eE]+)", curve_text, re.MULTILINE):
        curve_text = re.sub(
            r"(^[ \t]*Default:\s*)([-+0-9.eE]+)",
            lambda m: "{}{}".format(m.group(1), "{:.15g}".format(float(values[0]))),
            curve_text,
            count=1,
            flags=re.MULTILINE,
        )

    return curve_text


def _all_close(values, tol=1e-10):
    if not values:
        return True
    first = float(values[0])
    for v in values[1:]:
        if abs(float(v) - first) > tol:
            return False
    return True


def _set_camera_property_sampled(full_text, target_model_name, property_name, frames, values, fps):
    if not frames or len(frames) != len(values):
        raise ReframeBakeError(
            "Internal error: sampled frames/values mismatch for '{}'.".format(property_name)
        )

    _, curve_node_id, curve_id = _camera_property_curve_links(full_text, target_model_name, property_name)

    if _all_close(values):
        full_text = _patch_property_static(
            full_text,
            target_model_name,
            property_name,
            lambda _old: float(values[0]),
        )
        return full_text

    if curve_id is None or curve_node_id is None:
        raise ReframeBakeError(
            "Expected animated FBX curve for '{}', but none was found.".format(property_name)
        )

    objs, _, attr = _resolve_camera_blocks(full_text, target_model_name)
    curves = {x["id"]: x for x in objs["curves"]}
    curve_nodes = {x["id"]: x for x in objs["curve_nodes"]}

    curve_obj = curves[curve_id]
    curve_node = curve_nodes[curve_node_id]

    curve_new = _set_curve_block_sampled_values(curve_obj["text"], frames, values, fps)
    attr_new = _set_or_add_numeric_prop(attr["text"], property_name, float(values[0]))
    curve_node_new = _set_existing_prop(curve_node["text"], "d|" + property_name, float(values[0]))

    replacements = [
        (curve_obj["start"], curve_obj["end"], curve_new),
        (curve_node["start"], curve_node["end"], curve_node_new),
        (attr["start"], attr["end"], attr_new),
    ]
    for s, e, rep in sorted(replacements, key=lambda x: x[0], reverse=True):
        full_text = full_text[:s] + rep + full_text[e:]
    return full_text


def _apply_reframe_to_fbx_text(source_text, target_model_name, comp, dcc, force_static=False):
    scale = comp["scale"]
    text = source_text

    if force_static:
        apply_prop = lambda t, p, fn: _patch_property_static(t, target_model_name, p, fn)
    else:
        apply_prop = lambda t, p, fn: _patch_property_everywhere(t, target_model_name, p, fn)

    text = apply_prop(text, "FocalLength", lambda old: old * scale)

    if dcc == "nuke":
        dx = comp["nuke_delta_u"]
        dy = comp["nuke_delta_v"]
        text = apply_prop(text, "FilmOffsetX", lambda old: old + dx)
        text = apply_prop(text, "FilmOffsetY", lambda old: old + dy)
    elif dcc == "maya":
        dx = comp["delta_hfo_in"]
        dy = comp["delta_vfo_in"]
        text = apply_prop(text, "FilmOffsetX", lambda old: old + dx)
        text = apply_prop(text, "FilmOffsetY", lambda old: old + dy)
    else:
        dx = comp["delta_hfo_in"]
        dy = -comp["delta_vfo_in"]
        text = apply_prop(text, "FilmOffsetX", lambda old: old + dx)
        text = apply_prop(text, "FilmOffsetY", lambda old: old + dy)

    text = _remove_camera_vector_film_offset(text, target_model_name)

    if force_static:
        text = _remove_static_camera_animation(text, target_model_name)

    text = _remove_unreferenced_animation_objects(text)
    return text


def _unreal_film_offset_from_reframe(comp):
    return {
        "properties": ("FilmOffsetX", "FilmOffsetY"),
        "delta_x": comp["delta_hfo_in"],
        "delta_y": -comp["delta_vfo_in"],
        "units": "in",
    }


def _dcc_adjustments(comp):
    focal_old = comp["focal_mm"]
    focal_new = focal_old * comp["scale"]
    unreal_offsets = _unreal_film_offset_from_reframe(comp)
    return {
        "nuke": {
            "focal_old_mm": focal_old,
            "focal_new_mm": focal_new,
            "delta_x_win_translate": comp["nuke_delta_u"],
            "delta_y_win_translate": comp["nuke_delta_v"],
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


def _transform_knob_names():
    return ("translate", "center", "scale", "rotate", "skewX", "skewY")


def _camera_knob_names():
    return (
        "translate",
        "rotate",
        "scaling",
        "uniform_scale",
        "pivot",
        "focal",
        "haperture",
        "vaperture",
        "win_translate",
        "win_scale",
        "winroll",
        "near",
        "far",
    )


def _knob_channels(knob):
    for attr in ("arraySize", "numValues", "width"):
        try:
            n = int(getattr(knob, attr)())
            if n > 0:
                return n
        except Exception:
            pass
    return 1


def _extract_key_frame(key):
    for attr in ("x", "time", "frame"):
        try:
            value = getattr(key, attr)
            value = value() if callable(value) else value
            return int(round(float(value)))
        except Exception:
            pass
    return None


def _knob_key_frames(knob):
    frames = set()
    channels = max(1, _knob_channels(knob))
    for ch in range(channels):
        if not _is_animated(knob, ch):
            continue
        anim = None
        try:
            anim = knob.animation(ch)
        except Exception:
            try:
                anim = knob.animation()
            except Exception:
                anim = None
        if anim is None:
            continue
        try:
            keys = anim.keys()
        except Exception:
            keys = []
        for key in keys:
            fr = _extract_key_frame(key)
            if fr is not None:
                frames.add(fr)
    return frames


def _collect_relevant_keyframes(node, knob_names):
    frames = set()
    for name in knob_names:
        if name not in node.knobs():
            continue
        frames.update(_knob_key_frames(node[name]))
    return frames


def _node_has_relevant_animation(node, knob_names):
    for name in knob_names:
        if name in node.knobs() and _is_animated(node[name]):
            return True
    return False


def _camera_source_category(camera):
    if _camera_is_reading_from_file(camera):
        return "file_backed"
    if _node_has_relevant_animation(camera, _camera_knob_names()):
        return "animated_nuke"
    return "static_nuke"


def _output_requires_bake(transform, camera):
    if _camera_is_reading_from_file(camera):
        return False
    if _node_has_relevant_animation(transform, _transform_knob_names()):
        return True
    if _node_has_relevant_animation(camera, _camera_knob_names()):
        return True
    return False


def _get_bake_range(transform, camera):
    root_first = int(nuke.root()["first_frame"].value())
    root_last = int(nuke.root()["last_frame"].value())

    frames = {root_first, root_last}
    frames.update(_collect_relevant_keyframes(transform, _transform_knob_names()))
    frames.update(_collect_relevant_keyframes(camera, _camera_knob_names()))

    return min(frames), max(frames)


def _iter_frames(first_frame, last_frame):
    return range(int(first_frame), int(last_frame) + 1)


def _validate_transform_at_frame(node, frame=None):
    required = ("translate", "center", "scale")
    for name in required:
        if name not in node.knobs():
            raise ReframeBakeError("Transform is missing '{}' knob".format(name))

    if "rotate" in node.knobs():
        rot = _knob_value(node["rotate"], frame)
        if abs(rot) > EPS:
            extra = " at frame {}".format(frame) if frame is not None else ""
            raise ReframeBakeError("Transform.rotate must be 0{}.".format(extra))

    for name in ("skewX", "skewY"):
        if name in node.knobs():
            skew = _knob_value(node[name], frame)
            if abs(skew) > EPS:
                extra = " at frame {}".format(frame) if frame is not None else ""
                raise ReframeBakeError("Transform.{} must be 0{}.".format(name, extra))

    sx, sy = _knob_xy(node["scale"], frame)
    if abs(sx - sy) > 1e-6:
        extra = " at frame {}".format(frame) if frame is not None else ""
        raise ReframeBakeError(
            "Transform scale must be uniform (scale.x == scale.y){}.".format(extra)
        )
    if sx <= 0.0:
        extra = " at frame {}".format(frame) if frame is not None else ""
        raise ReframeBakeError("Transform scale must be > 0{}.".format(extra))


def _validate_transform(node, frames=None):
    if frames is None:
        _validate_transform_at_frame(node, frame=None)
        return
    for frame in frames:
        _validate_transform_at_frame(node, frame=frame)


def _validate_camera_at_frame(camera, frame=None):
    for name in ("focal", "haperture", "vaperture"):
        if name not in camera.knobs():
            raise ReframeBakeError("Camera missing '{}' knob".format(name))

    if "win_translate" in camera.knobs():
        wx, wy = _knob_xy(camera["win_translate"], frame)
        if abs(wx) > EPS or abs(wy) > EPS:
            extra = " at frame {}".format(frame) if frame is not None else ""
            raise ReframeBakeError("Camera.win_translate must be 0,0{}.".format(extra))
    if "win_scale" in camera.knobs():
        sx, sy = _knob_xy(camera["win_scale"], frame)
        if abs(sx - 1.0) > EPS or abs(sy - 1.0) > EPS:
            extra = " at frame {}".format(frame) if frame is not None else ""
            raise ReframeBakeError("Camera.win_scale must be 1,1{}.".format(extra))
    if "winroll" in camera.knobs():
        wr = _knob_value(camera["winroll"], frame)
        if abs(wr) > EPS:
            extra = " at frame {}".format(frame) if frame is not None else ""
            raise ReframeBakeError("Camera.winroll must be 0{}.".format(extra))


def _validate_camera(camera, frames=None):
    if frames is None:
        _validate_camera_at_frame(camera, frame=None)
        return
    for frame in frames:
        _validate_camera_at_frame(camera, frame=frame)


def _sample_reframe_inputs(transform, camera, frame=None):
    tx, ty = _knob_xy(transform["translate"], frame)
    cx, cy = _knob_xy(transform["center"], frame)
    sx, sy = _knob_xy(transform["scale"], frame)
    if abs(sx - sy) > 1e-6:
        raise ReframeBakeError("Transform scale must be uniform.")
    scale = sx
    if scale <= 0:
        raise ReframeBakeError("Transform scale must be > 0.")

    fmt = transform.input(0).format() if transform.input(0) else nuke.root().format()
    frame_w = float(fmt.width())
    frame_h = float(fmt.height())
    if frame_w <= 0 or frame_h <= 0:
        raise ReframeBakeError("Invalid frame format.")

    sensor_w_mm = _knob_value(camera["haperture"], frame)
    sensor_h_mm = _knob_value(camera["vaperture"], frame)
    focal_mm = _knob_value(camera["focal"], frame)

    return {
        "frame": frame,
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
    }


def _compute_reframe_from_inputs(sample):
    tx = sample["tx"]
    ty = sample["ty"]
    cx = sample["cx"]
    cy = sample["cy"]
    scale = sample["scale"]
    frame_w = sample["frame_w"]
    frame_h = sample["frame_h"]
    sensor_w_mm = sample["sensor_w_mm"]
    sensor_h_mm = sample["sensor_h_mm"]
    focal_mm = sample["focal_mm"]

    shift_x_px = tx + (1.0 - scale) * (cx - frame_w * 0.5)
    shift_y_px = ty + (1.0 - scale) * (cy - frame_h * 0.5)

    delta_u = shift_x_px / frame_w
    delta_v = shift_y_px / frame_h
    nuke_delta_u = delta_u * -2.0
    nuke_delta_v = delta_v / ((frame_w / frame_h) / -2.0)

    out = dict(sample)
    out.update(
        {
            "shift_x_px": shift_x_px,
            "shift_y_px": shift_y_px,
            "delta_u": delta_u,
            "delta_v": delta_v,
            "nuke_delta_u": nuke_delta_u,
            "nuke_delta_v": nuke_delta_v,
            "delta_hfo_in": delta_u * (sensor_w_mm * MM_TO_INCH),
            "delta_vfo_in": delta_v * (sensor_h_mm * MM_TO_INCH),
        }
    )
    return out


def _compute_reframe(transform, camera, frame=None):
    return _compute_reframe_from_inputs(_sample_reframe_inputs(transform, camera, frame=frame))


def _build_bake_samples(transform, camera, bake_first, bake_last):
    frames = []
    comps = []
    for frame in _iter_frames(bake_first, bake_last):
        frames.append(frame)
        comps.append(_compute_reframe(transform, camera, frame=frame))
    return frames, comps


def _fps():
    try:
        return float(nuke.root()["fps"].value())
    except Exception:
        return 24.0


def _export_camera_to_ascii_fbx(camera_node, out_path, start_frame, end_frame):
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

        for knob, val in (
            ("writeCameras", True),
            ("writeGeometries", False),
            ("writeLights", False),
            ("writeAxes", False),
            ("writePointClouds", False),
            ("asciiFileFormat", True),
        ):
            if knob in temp_write.knobs():
                temp_write[knob].setValue(val)

        nuke.execute(temp_write.name(), int(start_frame), int(end_frame))
    finally:
        for node in (temp_write, temp_scene):
            if node is not None:
                try:
                    nuke.delete(node)
                except Exception:
                    pass


def _camera_is_static_in_nuke(camera):
    return not _node_has_relevant_animation(camera, _camera_knob_names())


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
    return _ask(
        "The following files already exist and will be overwritten:\n\n{}\n\nContinue?".format(
            "\n".join(existing)
        )
    )


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


def _safe_reload(node):
    try:
        if "reload" in node.knobs():
            node["reload"].execute()
            return True
    except Exception:
        pass
    return False


def _duplicate_node(node):
    old_selection = nuke.selectedNodes()
    try:
        for n in old_selection:
            n.setSelected(False)

        node.setSelected(True)
        nuke.nodeCopy("%clipboard%")
        new_node = nuke.nodePaste("%clipboard%")

        for n in nuke.selectedNodes():
            n.setSelected(False)
        new_node.setSelected(True)
        return new_node
    finally:
        try:
            for n in nuke.selectedNodes():
                n.setSelected(False)
        except Exception:
            pass
        for n in old_selection:
            try:
                n.setSelected(True)
            except Exception:
                pass


def _position_duplicate_near_original(src_node, dup_node, x_offset=0, y_offset=30):
    try:
        dup_node.setXYpos(src_node.xpos() + x_offset, src_node.ypos() + y_offset)
    except Exception:
        pass


def _import_fbx_camera_with_duplicate_refresh(
    fbx_path,
    wanted_camera_name,
    node_base_name,
    xpos,
    ypos,
    delete_original=True,
    max_tries=30,
    delay_ms=100,
):
    cam = nuke.nodes.Camera2()
    try:
        cam.setName(node_base_name + "_reframed")
    except Exception:
        pass
    try:
        cam.setXpos(xpos)
        cam.setYpos(ypos)
    except Exception:
        pass

    if "read_from_file" in cam.knobs():
        cam["read_from_file"].setValue(True)
    cam["file"].setValue(_to_nuke_path(fbx_path))
    _safe_reload(cam)

    original_name = cam.fullName()
    state = {"tries": 0, "finished": False}

    def finish_with_duplicate():
        original = nuke.toNode(original_name)
        if original is None:
            return

        _safe_reload(original)
        try:
            dup = _duplicate_node(original)
        except Exception as exc:
            nuke.warning("Copy/paste refresh failed:\n{}".format(exc))
            return

        _position_duplicate_near_original(original, dup)
        try:
            wanted_name = original.name() + "_FBX"
            if dup.name() != wanted_name:
                dup.setName(wanted_name, uncollide=True)
        except Exception:
            pass

        _safe_reload(dup)
        try:
            names = _get_enum_names(dup["fbx_node_name"])
            if wanted_camera_name in names:
                try:
                    dup["fbx_node_name"].setValue(wanted_camera_name)
                except Exception:
                    dup["fbx_node_name"].setValue(names.index(wanted_camera_name))
                _safe_reload(dup)
        except Exception:
            pass

        if delete_original:
            try:
                nuke.delete(original)
            except Exception:
                pass
        try:
            dup.setSelected(True)
        except Exception:
            pass

    def try_set_camera_name():
        if state["finished"]:
            return
        state["tries"] += 1

        cam_now = nuke.toNode(original_name)
        if cam_now is None:
            return

        try:
            cam_now.forceValidate()
        except Exception:
            pass

        try:
            names = _get_enum_names(cam_now["fbx_node_name"])
        except Exception:
            names = []

        if wanted_camera_name in names:
            try:
                cam_now["fbx_node_name"].setValue(wanted_camera_name)
            except Exception:
                try:
                    cam_now["fbx_node_name"].setValue(names.index(wanted_camera_name))
                except Exception as exc:
                    nuke.warning(
                        "Found camera '{}', but could not set fbx_node_name:\n{}".format(
                            wanted_camera_name, exc
                        )
                    )
                    return
            state["finished"] = True
            QtCore.QTimer.singleShot(0, finish_with_duplicate)
            return

        if state["tries"] < max_tries:
            QtCore.QTimer.singleShot(delay_ms, try_set_camera_name)
        else:
            non_producer = [n for n in names if n not in PRODUCER_NAMES]
            nuke.warning(
                "Could not find FBX camera '{}'.\n\nAvailable names:\n{}\n\nNon-producer names:\n{}".format(
                    wanted_camera_name,
                    "\n".join(names) if names else "(none)",
                    "\n".join(non_producer) if non_producer else "(none)",
                )
            )

    QtCore.QTimer.singleShot(delay_ms, try_set_camera_name)
    return cam


def _create_imported_camera(file_path, target_name, ref_node):
    return _import_fbx_camera_with_duplicate_refresh(
        fbx_path=file_path,
        wanted_camera_name=target_name,
        node_base_name=ref_node.name(),
        xpos=ref_node.xpos() + 100,
        ypos=ref_node.ypos() + 100,
        delete_original=True,
        max_tries=30,
        delay_ms=100,
    )


def _clear_knob_animation(knob, channel=None):
    try:
        if channel is None:
            knob.clearAnimated()
        else:
            knob.clearAnimated(channel)
        return
    except Exception:
        pass
    try:
        channels = _knob_channels(knob) if channel is None else 1
        if channel is None:
            for ch in range(channels):
                try:
                    knob.clearAnimated(ch)
                except Exception:
                    pass
    except Exception:
        pass


def _set_scalar_samples(knob, frames, values):
    if _all_close(values):
        _clear_knob_animation(knob)
        knob.setValue(float(values[0]))
        return
    _clear_knob_animation(knob)
    try:
        knob.setAnimated()
    except Exception:
        pass
    for frame, value in zip(frames, values):
        knob.setValueAt(float(value), int(frame))


def _set_xy_samples(knob, frames, xs, ys):
    if _all_close(xs):
        _clear_knob_animation(knob, 0)
        knob.setValue(float(xs[0]), 0)
    else:
        _clear_knob_animation(knob, 0)
        try:
            knob.setAnimated(0)
        except Exception:
            pass
        for frame, value in zip(frames, xs):
            knob.setValueAt(float(value), int(frame), 0)

    if _all_close(ys):
        _clear_knob_animation(knob, 1)
        knob.setValue(float(ys[0]), 1)
    else:
        _clear_knob_animation(knob, 1)
        try:
            knob.setAnimated(1)
        except Exception:
            pass
        for frame, value in zip(frames, ys):
            knob.setValueAt(float(value), int(frame), 1)


def _build_dcc_samples_from_comps(comps):
    out = {
        "nuke": {
            "focal": [],
            "film_x": [],
            "film_y": [],
        },
        "maya": {
            "focal": [],
            "film_x": [],
            "film_y": [],
        },
        "unreal": {
            "focal": [],
            "film_x": [],
            "film_y": [],
        },
    }
    for comp in comps:
        focal = comp["focal_mm"] * comp["scale"]
        out["nuke"]["focal"].append(focal)
        out["nuke"]["film_x"].append(comp["nuke_delta_u"])
        out["nuke"]["film_y"].append(comp["nuke_delta_v"])

        out["maya"]["focal"].append(focal)
        out["maya"]["film_x"].append(comp["delta_hfo_in"])
        out["maya"]["film_y"].append(comp["delta_vfo_in"])

        out["unreal"]["focal"].append(focal)
        out["unreal"]["film_x"].append(comp["delta_hfo_in"])
        out["unreal"]["film_y"].append(-comp["delta_vfo_in"])
    return out


def _make_temp_baked_camera(transform, camera, bake_first, bake_last):
    temp_camera = _duplicate_node(camera)
    _position_duplicate_near_original(camera, temp_camera, x_offset=180, y_offset=120)
    try:
        temp_camera.setName(camera.name() + "_TEMP_BAKE", uncollide=True)
    except Exception:
        pass

    if "read_from_file" in temp_camera.knobs():
        try:
            temp_camera["read_from_file"].setValue(False)
        except Exception:
            pass
    if "file" in temp_camera.knobs():
        try:
            temp_camera["file"].setValue("")
        except Exception:
            pass

    frames, comps = _build_bake_samples(transform, camera, bake_first, bake_last)
    dcc_samples = _build_dcc_samples_from_comps(comps)

    _set_scalar_samples(temp_camera["focal"], frames, dcc_samples["nuke"]["focal"])
    if "win_translate" not in temp_camera.knobs():
        raise ReframeBakeError("Camera is missing win_translate knob.")
    _set_xy_samples(
        temp_camera["win_translate"],
        frames,
        dcc_samples["nuke"]["film_x"],
        dcc_samples["nuke"]["film_y"],
    )

    return temp_camera, frames, comps, dcc_samples


def _summary_text(
    transform,
    camera,
    comp,
    out_dir,
    outs,
    source_mode,
    source_fbx,
    bake_mode,
    bake_first=None,
    bake_last=None,
):
    dcc = _dcc_adjustments(comp)
    lines = [
        "CameraCrop reframe bake",
        "----------------------",
        "Transform: {}".format(transform.name()),
        "Camera:    {}".format(camera.name()),
        "Source:    {}".format(source_mode),
        "Output:    {}".format("sampled bake" if bake_mode else "static patch"),
        "Source FBX: {}".format(source_fbx if source_fbx else "(will export from camera node)"),
    ]
    if bake_first is not None and bake_last is not None:
        lines.extend(
            [
                "Bake range: {} - {}".format(int(bake_first), int(bake_last)),
            ]
        )
    lines.extend(
        [
            "",
            "Frame size: {} x {}".format(int(comp["frame_w"]), int(comp["frame_h"])),
            "Translate: ({:.6f}, {:.6f}) px".format(comp["tx"], comp["ty"]),
            "Center:    ({:.6f}, {:.6f}) px".format(comp["cx"], comp["cy"]),
            "Scale:     {:.8f}".format(comp["scale"]),
            "",
            "Per-DCC adjustments at the current sample:",
            "  Nuke   -> Focal: {:.6f} -> {:.6f} mm, win_translate delta: ({:.10f}, {:.10f}) normalized".format(
                dcc["nuke"]["focal_old_mm"],
                dcc["nuke"]["focal_new_mm"],
                dcc["nuke"]["delta_x_win_translate"],
                dcc["nuke"]["delta_y_win_translate"],
            ),
            "  Maya   -> Focal: {:.6f} -> {:.6f} mm, FilmOffset delta: ({:.10f}, {:.10f}) in".format(
                dcc["maya"]["focal_old_mm"],
                dcc["maya"]["focal_new_mm"],
                dcc["maya"]["delta_x_in"],
                dcc["maya"]["delta_y_in"],
            ),
            "  Unreal -> Focal: {:.6f} -> {:.6f} mm, FilmOffset delta: ({:.10f}, {:.10f}) {} (FBX FilmOffsetX/Y mapping, Y inverted)".format(
                dcc["unreal"]["focal_old_mm"],
                dcc["unreal"]["focal_new_mm"],
                dcc["unreal"]["delta_x"],
                dcc["unreal"]["delta_y"],
                dcc["unreal"]["units"],
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
    )
    return "\n".join(lines)


def _build_outputs_from_baked_source(
    source_text,
    exported_camera_name,
    final_camera_name,
    frames,
    dcc_samples,
):
    fps = _fps()
    outputs = {}

    for dcc in ("nuke", "maya", "unreal"):
        text = source_text
        text = _rename_exported_camera(text, exported_camera_name, final_camera_name)

        if dcc != "nuke":
            text = _set_camera_property_sampled(
                text,
                final_camera_name,
                "FilmOffsetX",
                frames,
                dcc_samples[dcc]["film_x"],
                fps,
            )
            text = _set_camera_property_sampled(
                text,
                final_camera_name,
                "FilmOffsetY",
                frames,
                dcc_samples[dcc]["film_y"],
                fps,
            )

        text = _remove_camera_vector_film_offset(text, final_camera_name)
        if dcc == "nuke":
            text = _set_default_camera_in_fbx_text(text, final_camera_name)
        text = _remove_unreferenced_animation_objects(text)
        outputs[dcc] = text

    return outputs


def bake_selected_transform_into_cameras():
    transform, camera = _selected_transform_and_camera()
    selected_camera_name = camera.name()

    source_fbx = ""
    source_mode = _camera_source_category(camera)
    bake_mode = _output_requires_bake(transform, camera)

    bake_first = None
    bake_last = None
    validation_frames = None
    if bake_mode:
        bake_first, bake_last = _get_bake_range(transform, camera)
        validation_frames = _iter_frames(bake_first, bake_last)

    _validate_transform(transform, frames=validation_frames)
    _validate_camera(camera, frames=validation_frames)

    if source_mode == "file_backed":
        source_fbx = _evaluate_file_knob(camera, "file")
        if not source_fbx:
            raise ReframeBakeError("Camera is set to read from file, but file path is empty.")
        if not os.path.exists(source_fbx):
            raise ReframeBakeError("FBX file does not exist:\n{}".format(source_fbx))
        if not source_fbx.lower().endswith(".fbx"):
            raise ReframeBakeError("Camera file must be .fbx")
        if not _is_ascii_fbx(source_fbx):
            raise ReframeBakeError("Only ASCII FBX is supported for read-from-file cameras.")

    comp = _compute_reframe(transform, camera, frame=nuke.frame())

    default_dir = _default_output_dir(camera)
    suggested_base = (
        os.path.splitext(os.path.basename(source_fbx))[0] if source_fbx else camera.name()
    )
    out_dir, base_name = _ask_output_plan(default_dir, suggested_base)
    if not out_dir:
        return

    output_names = _new_output_names(base_name)
    output_paths = {k: _to_nuke_path(os.path.join(out_dir, v)) for k, v in output_names.items()}

    summary = _summary_text(
        transform,
        camera,
        comp,
        out_dir,
        output_names,
        source_mode,
        source_fbx,
        bake_mode,
        bake_first=bake_first,
        bake_last=bake_last,
    )
    print(summary)
    if not _ask(summary + "\n\nProceed?"):
        return

    if not _confirm_overwrite(list(output_paths.values())):
        return

    target_model_name = _resolve_target_model_name(camera)

    if source_mode == "file_backed":
        source_text = _read_text(source_fbx)
        source_is_static = not any(
            [
                _camera_property_has_animation(source_text, target_model_name, "FocalLength"),
                _camera_property_has_animation(source_text, target_model_name, "FilmOffsetX"),
                _camera_property_has_animation(source_text, target_model_name, "FilmOffsetY"),
            ]
        )
        force_static_rewrite = source_is_static
        outputs = {}
        for dcc in ("nuke", "maya", "unreal"):
            patched = _apply_reframe_to_fbx_text(
                source_text,
                target_model_name,
                comp,
                dcc,
                force_static=force_static_rewrite,
            )
            if dcc == "nuke":
                patched = _set_default_camera_in_fbx_text(patched, selected_camera_name)
            outputs[dcc] = patched
    elif bake_mode:
        temp_src = _to_nuke_path(
            os.path.join(out_dir, output_names["nuke"] + ".tmp_export_baked_source.fbx")
        )
        temp_camera = None
        try:
            temp_camera, frames, comps, dcc_samples = _make_temp_baked_camera(
                transform,
                camera,
                bake_first,
                bake_last,
            )
            exported_camera_name = temp_camera.name()
            _export_camera_to_ascii_fbx(temp_camera, temp_src, bake_first, bake_last)
            if not os.path.exists(temp_src):
                raise ReframeBakeError("Failed to export baked source FBX from camera.")
            if not _is_ascii_fbx(temp_src):
                raise ReframeBakeError("Exported baked camera FBX is binary; expected ASCII.")
            source_text = _read_text(temp_src)
            outputs = _build_outputs_from_baked_source(
                source_text,
                exported_camera_name,
                selected_camera_name,
                frames,
                dcc_samples,
            )
        finally:
            try:
                if temp_camera is not None:
                    nuke.delete(temp_camera)
            except Exception:
                pass
            try:
                if os.path.exists(temp_src):
                    os.remove(temp_src)
            except Exception:
                pass
    else:
        temp_src = _to_nuke_path(
            os.path.join(out_dir, output_names["nuke"] + ".tmp_export_source.fbx")
        )
        _export_camera_to_ascii_fbx(camera, temp_src, int(nuke.frame()), int(nuke.frame()))
        if not os.path.exists(temp_src):
            raise ReframeBakeError("Failed to export source FBX from camera.")
        if not _is_ascii_fbx(temp_src):
            raise ReframeBakeError("Exported camera FBX is binary; expected ASCII.")
        source_text = _read_text(temp_src)
        try:
            os.remove(temp_src)
        except Exception:
            pass

        force_static_rewrite = True
        outputs = {}
        for dcc in ("nuke", "maya", "unreal"):
            patched = _apply_reframe_to_fbx_text(
                source_text,
                target_model_name,
                comp,
                dcc,
                force_static=force_static_rewrite,
            )
            if dcc == "nuke":
                patched = _set_default_camera_in_fbx_text(patched, selected_camera_name)
            outputs[dcc] = patched

    for dcc in ("nuke", "maya", "unreal"):
        _write_text(output_paths[dcc], outputs[dcc])

    imported = _create_imported_camera(output_paths["nuke"], selected_camera_name, camera)

    _msg(
        "Success.\n\n"
        "Created FBX files:\n"
        "- {}\n- {}\n- {}\n\n"
        "Imported verification camera: {}".format(
            output_paths["nuke"],
            output_paths["maya"],
            output_paths["unreal"],
            imported.name(),
        )
    )


try:
    bake_selected_transform_into_cameras()
except ReframeBakeError as exc:
    _msg("Reframe bake failed:\n\n{}".format(exc))
    raise
except Exception as exc:
    _msg("Unexpected error:\n\n{}".format(exc))
    raise
