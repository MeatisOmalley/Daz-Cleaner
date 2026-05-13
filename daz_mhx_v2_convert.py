import json
import os
import re
from collections import defaultdict, deque

import bpy
from bpy.app.handlers import persistent
from mathutils import Matrix, Quaternion, Vector


bl_info = {
    "name": "Daz MHX V2 Converter",
    "author": "Codex",
    "version": (0, 2, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Daz MHX",
    "description": "Aggressively convert bone-only MHX custom property morphs to cached driver-bone transforms.",
    "category": "Object",
}


OUTPUT_DIR = r"C:\Users\meat\Documents\blender\code\Daz Cleaner"
POSE_BONE_PATH_RE = re.compile(r'pose\.bones\["([^"]+)"\]\.(.+)')
CUSTOM_PROPERTY_PATH_RE = re.compile(r'\["([^"]+)"\]')
SAFE_CHANNELS = {"location", "rotation_euler", "rotation_quaternion", "scale"}

_RUNTIME_CACHES = {}
_APPLYING = False


def selected_armature(context):
    obj = context.object
    if obj and obj.type == "ARMATURE":
        return obj

    selected = [item for item in context.selected_objects if item.type == "ARMATURE"]
    return selected[0] if selected else None


def safe_filename(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "Armature"


def cache_path_for_armature(armature):
    return os.path.join(
        OUTPUT_DIR,
        f"daz_mhx_bone_morph_cache_{safe_filename(armature.name)}.json",
    )


def runtime_key_for_armature(armature, path):
    return (armature.as_pointer(), path)


def scene_frame_key(scene):
    if not scene:
        return None
    return (scene.frame_current, round(scene.frame_subframe, 6))


def id_prop_path(name):
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'["{escaped}"]'


def custom_properties_from_path(data_path):
    return CUSTOM_PROPERTY_PATH_RE.findall(data_path or "")


def parse_pose_bone_data_path(data_path):
    match = POSE_BONE_PATH_RE.match(data_path or "")
    if not match:
        return None

    bone_name, path_tail = match.groups()
    channel = path_tail.split(".", 1)[0]
    channel_base = channel.split("[", 1)[0]
    return {
        "bone_name": bone_name,
        "channel": channel,
        "channel_base": channel_base,
        "is_driver_bone": "(drv)" in bone_name,
    }


def prop_key(scope, name):
    return f"{scope}:{name}"


def split_prop_key(key):
    scope, name = key.split(":", 1)
    return scope, name


def id_block_for_scope(armature, scope):
    if scope == "object":
        return armature
    if scope == "data":
        return armature.data
    return None


def scope_for_id_block(armature, id_block):
    if id_block == armature:
        return "object"
    if id_block == armature.data:
        return "data"
    return None


def custom_property_keys(id_block):
    if not id_block:
        return []
    try:
        return [key for key in id_block.keys() if key != "_RNA_UI"]
    except TypeError:
        return []


def to_plain_value(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "to_list"):
        return value.to_list()
    if isinstance(value, dict):
        return {str(key): to_plain_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain_value(item) for item in value]
    try:
        return list(value)
    except TypeError:
        return str(value)


def numeric_scalar(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def property_ui_data(id_block, name):
    try:
        return to_plain_value(id_block.id_properties_ui(name).as_dict())
    except Exception:
        return {}


def update_property_ui(id_block, name, ui):
    if not isinstance(ui, dict):
        return
    if not ui:
        return

    allowed = {
        "min",
        "max",
        "soft_min",
        "soft_max",
        "description",
        "default",
        "step",
        "precision",
        "subtype",
    }
    data = {key: value for key, value in ui.items() if key in allowed and value is not None}
    if not data:
        return

    try:
        id_block.id_properties_ui(name).update(**data)
    except Exception:
        pass


def collect_prop_defs(armature):
    props = {}
    for scope in ("object", "data"):
        id_block = id_block_for_scope(armature, scope)
        for name in custom_property_keys(id_block):
            value = id_block[name]
            key = prop_key(scope, name)
            props[key] = {
                "scope": scope,
                "name": name,
                "value": to_plain_value(value),
                "value_type": type(value).__name__,
                "is_numeric_scalar": numeric_scalar(value),
                "ui": property_ui_data(id_block, name),
            }
    return props


def source_prop_refs_from_driver(armature, driver):
    refs = set()
    for variable in driver.variables:
        for target in variable.targets:
            scope = scope_for_id_block(armature, getattr(target, "id", None))
            if not scope:
                continue
            for name in custom_properties_from_path(getattr(target, "data_path", "")):
                refs.add(prop_key(scope, name))
    return refs


def output_prop_ref_from_fcurve(armature, owner_scope, fcurve):
    names = custom_properties_from_path(fcurve.data_path)
    if not names:
        return None
    id_block = id_block_for_scope(armature, owner_scope)
    if not id_block or names[0] not in id_block:
        return None
    return prop_key(owner_scope, names[0])


def driver_records_for_owner(armature, owner_scope):
    id_block = id_block_for_scope(armature, owner_scope)
    if not id_block or not id_block.animation_data:
        return []

    return [
        {
            "owner_scope": owner_scope,
            "fcurve": fcurve,
            "data_path": fcurve.data_path,
            "array_index": fcurve.array_index,
            "source_props": source_prop_refs_from_driver(armature, fcurve.driver),
            "output_prop": output_prop_ref_from_fcurve(armature, owner_scope, fcurve),
            "pose_path": parse_pose_bone_data_path(fcurve.data_path),
        }
        for fcurve in id_block.animation_data.drivers
    ]


def related_meshes(armature):
    meshes = []
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        if obj.parent == armature:
            meshes.append(obj)
            continue
        for modifier in obj.modifiers:
            if modifier.type == "ARMATURE" and getattr(modifier, "object", None) == armature:
                meshes.append(obj)
                break
    return meshes


def shape_key_source_props(armature):
    refs = set()
    drivers = []
    for mesh in related_meshes(armature):
        shape_keys = mesh.data.shape_keys
        if not shape_keys or not shape_keys.animation_data:
            continue
        for fcurve in shape_keys.animation_data.drivers:
            source_refs = source_prop_refs_from_driver(armature, fcurve.driver)
            refs.update(source_refs)
            drivers.append(
                {
                    "mesh": mesh.name,
                    "shape_keys": shape_keys.name,
                    "data_path": fcurve.data_path,
                    "array_index": fcurve.array_index,
                    "source_props": sorted(source_refs),
                }
            )
    return refs, drivers


def copy_transform_links_by_driver_bone(armature):
    links = {}
    for pose_bone in armature.pose.bones:
        for index, constraint in enumerate(pose_bone.constraints):
            if constraint.type != "COPY_TRANSFORMS":
                continue

            target_bone = getattr(constraint, "subtarget", "")
            is_safe = (
                getattr(constraint, "target", None) == armature
                and "(drv)" in target_bone
                and constraint.owner_space == "LOCAL"
                and constraint.target_space == "LOCAL"
                and abs(constraint.influence - 1.0) < 0.0001
                and not constraint.mute
            )
            if not is_safe:
                continue

            links.setdefault(target_bone, []).append(
                {
                    "author_bone": pose_bone.name,
                    "constraint_index": index,
                    "constraint_name": constraint.name,
                }
            )
    return links


def bone_transform_source_props(armature, driver_records):
    refs = set()
    transform_drivers = []
    links_by_driver_bone = copy_transform_links_by_driver_bone(armature)
    for record in driver_records:
        pose_path = record["pose_path"]
        if not pose_path:
            continue
        if pose_path["channel_base"] not in SAFE_CHANNELS:
            continue
        if not pose_path["is_driver_bone"]:
            continue
        if pose_path["bone_name"] not in links_by_driver_bone:
            continue

        refs.update(record["source_props"])
        transform_drivers.append(
            {
                "data_path": record["data_path"],
                "array_index": record["array_index"],
                "driver_bone": pose_path["bone_name"],
                "channel": pose_path["channel_base"],
                "source_props": sorted(record["source_props"]),
            }
        )
    return refs, transform_drivers


def ancestor_props(seed_props, reverse_edges):
    found = set(seed_props)
    queue = deque(seed_props)
    while queue:
        prop = queue.popleft()
        for parent in reverse_edges.get(prop, set()):
            if parent in found:
                continue
            found.add(parent)
            queue.append(parent)
    return found


def build_classification(armature):
    prop_defs = collect_prop_defs(armature)
    object_drivers = driver_records_for_owner(armature, "object")
    data_drivers = driver_records_for_owner(armature, "data")
    all_prop_drivers = object_drivers + data_drivers

    edges = defaultdict(set)
    reverse_edges = defaultdict(set)
    driven_props = set()
    for record in all_prop_drivers:
        output_prop = record["output_prop"]
        if not output_prop:
            continue
        driven_props.add(output_prop)
        for source_prop in record["source_props"]:
            edges[source_prop].add(output_prop)
            reverse_edges[output_prop].add(source_prop)

    shape_seeds, shape_drivers = shape_key_source_props(armature)
    bone_seeds, transform_drivers = bone_transform_source_props(armature, object_drivers)
    shape_touched = ancestor_props(shape_seeds, reverse_edges)
    bone_touched = ancestor_props(bone_seeds, reverse_edges)

    controls = []
    internal_delete_props = []
    protected_props = []
    for key, prop_def in sorted(prop_defs.items()):
        reaches_bones = key in bone_touched
        reaches_shape_keys = key in shape_touched
        is_driven = key in driven_props
        is_control = (
            reaches_bones
            and not reaches_shape_keys
            and not is_driven
            and prop_def["is_numeric_scalar"]
        )

        entry = dict(prop_def)
        entry.update(
            {
                "key": key,
                "reaches_bones": reaches_bones,
                "reaches_shape_keys": reaches_shape_keys,
                "is_driven": is_driven,
            }
        )

        if is_control:
            controls.append(entry)
        elif reaches_bones and not reaches_shape_keys:
            internal_delete_props.append(entry)
        elif reaches_shape_keys:
            protected_props.append(entry)

    return {
        "prop_defs": prop_defs,
        "controls": controls,
        "internal_delete_props": internal_delete_props,
        "protected_props": protected_props,
        "bone_touched_props": sorted(bone_touched),
        "shape_touched_props": sorted(shape_touched),
        "driven_props": sorted(driven_props),
        "prop_driver_records": all_prop_drivers,
        "shape_drivers": shape_drivers,
        "transform_drivers": transform_drivers,
    }


def debug_prop_list_entry(item):
    ui = item.get("ui", {})
    if not isinstance(ui, dict):
        ui = {}
    return {
        "key": item["key"],
        "scope": item["scope"],
        "name": item["name"],
        "is_driven": item.get("is_driven", False),
        "is_numeric_scalar": item.get("is_numeric_scalar", False),
        "reaches_bones": item.get("reaches_bones", False),
        "reaches_shape_keys": item.get("reaches_shape_keys", False),
        "ui": ui,
    }


def sorted_debug_prop_list(items):
    return [
        debug_prop_list_entry(item)
        for item in sorted(
            items,
            key=lambda item: (
                item.get("name", "").lower(),
                item.get("scope", ""),
                item.get("key", ""),
            ),
        )
    ]


def debug_property_lists(classification):
    bone_only = [
        item
        for item in classification["controls"] + classification["internal_delete_props"]
        if item.get("reaches_bones") and not item.get("reaches_shape_keys")
    ]
    mixed = [
        item
        for item in classification["protected_props"]
        if item.get("reaches_bones") and item.get("reaches_shape_keys")
    ]
    shape_key_only = [
        item
        for item in classification["protected_props"]
        if item.get("reaches_shape_keys") and not item.get("reaches_bones")
    ]

    return {
        "bone_morphs": sorted_debug_prop_list(bone_only),
        "mixed_morphs": sorted_debug_prop_list(mixed),
        "shapekey_morphs": sorted_debug_prop_list(shape_key_only),
    }


def transform_snapshot(pose_bone):
    return {
        "matrix_basis": [list(row) for row in pose_bone.matrix_basis],
    }


def matrix_from_plain(rows):
    return Matrix(rows)


def matrix_to_plain(matrix):
    return [list(row) for row in matrix]


def force_depsgraph_update(context):
    for obj in context.selected_objects:
        obj.update_tag()
    context.view_layer.update()
    context.evaluated_depsgraph_get().update()


def snapshot_driver_bones(context, armature, driver_bones):
    force_depsgraph_update(context)
    depsgraph = context.evaluated_depsgraph_get()
    evaluated = armature.evaluated_get(depsgraph)
    return {
        bone_name: transform_snapshot(evaluated.pose.bones[bone_name])
        for bone_name in driver_bones
        if bone_name in evaluated.pose.bones
    }


def max_matrix_diff(a, b):
    a_values = [value for row in a["matrix_basis"] for value in row]
    b_values = [value for row in b["matrix_basis"] for value in row]
    return max(abs(x - y) for x, y in zip(a_values, b_values))


def sample_value_for_control(control, direction):
    ui = control.get("ui", {})
    if not isinstance(ui, dict):
        ui = {}
    if direction > 0:
        max_value = ui.get("max", ui.get("soft_max", 1.0))
        try:
            return min(1.0, float(max_value)) if float(max_value) > 0 else 1.0
        except Exception:
            return 1.0

    min_value = ui.get("min", ui.get("soft_min", -1.0))
    try:
        return max(-1.0, float(min_value)) if float(min_value) < 0 else None
    except Exception:
        return -1.0


def get_prop_value(armature, key):
    scope, name = split_prop_key(key)
    return id_block_for_scope(armature, scope).get(name, 0.0)


def set_prop_value(armature, key, value):
    scope, name = split_prop_key(key)
    id_block_for_scope(armature, scope)[name] = value


def restore_values(armature, values):
    for key, value in values.items():
        set_prop_value(armature, key, value)


def bake_controls(context, armature, classification):
    links_by_driver_bone = copy_transform_links_by_driver_bone(armature)
    all_driver_bones = sorted(links_by_driver_bone)
    controls = classification["controls"]
    original_values = {
        control["key"]: get_prop_value(armature, control["key"])
        for control in controls
    }

    for control in controls:
        set_prop_value(armature, control["key"], 0.0)
    neutral = snapshot_driver_bones(context, armature, all_driver_bones)

    morphs = []
    affected_driver_bones = set()
    for control in controls:
        key = control["key"]
        poses = {}
        for pose_name, direction in (("positive", 1), ("negative", -1)):
            sample_value = sample_value_for_control(control, direction)
            if sample_value is None:
                continue

            set_prop_value(armature, key, sample_value)
            posed = snapshot_driver_bones(context, armature, all_driver_bones)
            changed = {
                bone_name: snapshot
                for bone_name, snapshot in posed.items()
                if bone_name in neutral
                and max_matrix_diff(neutral[bone_name], snapshot) > 0.000001
            }
            set_prop_value(armature, key, 0.0)
            if not changed:
                continue

            affected_driver_bones.update(changed)
            poses[pose_name] = {
                "source_value": sample_value,
                "driver_bases": changed,
            }

        if poses:
            ui = control["ui"] if isinstance(control.get("ui"), dict) else {}
            morphs.append(
                {
                    "key": key,
                    "scope": control["scope"],
                    "name": control["name"],
                    "ui": ui,
                    "default": ui.get("default", 0.0),
                    "original_value": control["value"],
                    "poses": poses,
                }
            )

    restore_values(armature, original_values)
    force_depsgraph_update(context)
    return {
        "neutral_driver_bases": {
            bone_name: neutral[bone_name]
            for bone_name in sorted(affected_driver_bones)
            if bone_name in neutral
        },
        "morphs": morphs,
        "affected_driver_bones": sorted(affected_driver_bones),
    }


def delete_transform_drivers(armature, affected_driver_bones):
    if not armature.animation_data:
        return 0

    removed = 0
    for fcurve in list(armature.animation_data.drivers):
        pose_path = parse_pose_bone_data_path(fcurve.data_path)
        if not pose_path:
            continue
        if pose_path["bone_name"] not in affected_driver_bones:
            continue
        if pose_path["channel_base"] not in SAFE_CHANNELS:
            continue
        armature.animation_data.drivers.remove(fcurve)
        removed += 1
    return removed


def remove_prop_driver(id_block, name):
    if not id_block or not id_block.animation_data:
        return 0

    removed = 0
    for fcurve in list(id_block.animation_data.drivers):
        names = custom_properties_from_path(fcurve.data_path)
        if names and names[0] == name:
            id_block.animation_data.drivers.remove(fcurve)
            removed += 1
    return removed


def delete_and_rebuild_props(armature, cache, classification):
    removed_prop_drivers = 0
    deleted_props = 0
    rebuild_controls = {
        prop_key(morph["scope"], morph["name"]): morph
        for morph in cache["morphs"]
    }
    delete_keys = set(rebuild_controls)
    delete_keys.update(item["key"] for item in classification["internal_delete_props"])

    for key in sorted(delete_keys):
        scope, name = split_prop_key(key)
        id_block = id_block_for_scope(armature, scope)
        if not id_block or name not in id_block:
            continue

        removed_prop_drivers += remove_prop_driver(id_block, name)
        try:
            del id_block[name]
            deleted_props += 1
        except Exception:
            pass

    for key, morph in sorted(rebuild_controls.items()):
        id_block = id_block_for_scope(armature, morph["scope"])
        default = morph.get("default", 0.0)
        id_block[morph["name"]] = default if numeric_scalar(default) else 0.0
        update_property_ui(id_block, morph["name"], morph.get("ui", {}))

    return removed_prop_drivers, deleted_props, len(rebuild_controls)


def make_cache(context, armature):
    classification = build_classification(armature)
    baked = bake_controls(context, armature, classification)
    debug_lists = debug_property_lists(classification)
    return {
        "schema_version": 2,
        "kind": "daz_mhx_bone_morph_cache",
        "armature": armature.name,
        "cache_id": armature.name,
        "neutral_driver_bases": baked["neutral_driver_bases"],
        "affected_driver_bones": baked["affected_driver_bones"],
        "morphs": baked["morphs"],
        "classification_summary": {
            "controls": len(classification["controls"]),
            "baked_morphs": len(baked["morphs"]),
            "internal_delete_props": len(classification["internal_delete_props"]),
            "protected_shape_key_props": len(classification["protected_props"]),
            "bone_morph_props": len(debug_lists["bone_morphs"]),
            "mixed_morph_props": len(debug_lists["mixed_morphs"]),
            "shapekey_morph_props": len(debug_lists["shapekey_morphs"]),
            "shape_key_drivers": len(classification["shape_drivers"]),
            "bone_transform_drivers": len(classification["transform_drivers"]),
        },
        "debug_property_lists": debug_lists,
        "protected_shape_key_props": [
            {
                "key": item["key"],
                "scope": item["scope"],
                "name": item["name"],
                "reaches_bones": item["reaches_bones"],
            }
            for item in classification["protected_props"]
        ],
        "internal_delete_props": [
            {
                "key": item["key"],
                "scope": item["scope"],
                "name": item["name"],
                "is_driven": item["is_driven"],
            }
            for item in classification["internal_delete_props"]
        ],
        "shape_drivers": classification["shape_drivers"],
    }, classification


def matrix_components(rows):
    loc, rot, scale = matrix_from_plain(rows).decompose()
    return loc, rot, scale


def delta_components(neutral_snapshot, posed_snapshot):
    neutral = matrix_from_plain(neutral_snapshot["matrix_basis"])
    posed = matrix_from_plain(posed_snapshot["matrix_basis"])
    delta = neutral.inverted() @ posed
    loc, rot, scale = delta.decompose()
    return {
        "loc": loc,
        "rot": rot,
        "scale": scale,
    }


def runtime_cache_for_armature(armature, force_reload=False):
    path = armature.get("daz_mhx_v2_cache_path", cache_path_for_armature(armature))
    modified_time = os.path.getmtime(path) if os.path.exists(path) else None
    cache_key = runtime_key_for_armature(armature, path)
    existing = _RUNTIME_CACHES.get(cache_key)
    if (
        existing
        and not force_reload
        and existing["modified_time"] == modified_time
    ):
        return existing
    if not modified_time:
        return None

    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    neutral_bones = {}
    for bone_name, snapshot in data.get("neutral_driver_bases", {}).items():
        pose_bone = armature.pose.bones.get(bone_name)
        if not pose_bone:
            continue
        neutral_bones[bone_name] = {
            "pose_bone": pose_bone,
            "matrix": matrix_from_plain(snapshot["matrix_basis"]),
            "snapshot": snapshot,
        }

    morphs = []
    for morph in data.get("morphs", []):
        scope = morph.get("scope")
        name = morph.get("name")
        id_block = id_block_for_scope(armature, scope)
        if not id_block or name not in id_block:
            continue

        runtime_morph = {
            "scope": scope,
            "name": name,
            "key": prop_key(scope, name),
            "id_block": id_block,
            "last_value": None,
            "poses": {},
        }
        for pose_name, pose in morph.get("poses", {}).items():
            source_value = pose.get("source_value", 1.0)
            bone_deltas = {}
            for bone_name, posed_snapshot in pose.get("driver_bases", {}).items():
                neutral = neutral_bones.get(bone_name)
                if not neutral:
                    continue
                bone_deltas[bone_name] = delta_components(
                    neutral["snapshot"],
                    posed_snapshot,
                )
            runtime_morph["poses"][pose_name] = {
                "source_value": source_value,
                "bone_deltas": bone_deltas,
            }
        morphs.append(runtime_morph)

    runtime = {
        "path": path,
        "modified_time": modified_time,
        "data": data,
        "neutral_bones": neutral_bones,
        "morphs": morphs,
        "last_frame_key": None,
    }
    _RUNTIME_CACHES[cache_key] = runtime
    return runtime


def weighted_delta_matrix(delta, factor):
    factor = max(0.0, min(1.0, factor))
    loc = Vector((0.0, 0.0, 0.0)).lerp(delta["loc"], factor)
    rot = Quaternion().slerp(delta["rot"], factor)
    scale = Vector((1.0, 1.0, 1.0)).lerp(delta["scale"], factor)
    return Matrix.LocRotScale(loc, rot, scale)


def apply_runtime_cache(armature, runtime, scene=None, force=False):
    matrices = {
        bone_name: item["matrix"].copy()
        for bone_name, item in runtime["neutral_bones"].items()
    }

    current_frame_key = scene_frame_key(scene)
    changed = force or current_frame_key != runtime.get("last_frame_key")
    active = 0
    for morph in runtime["morphs"]:
        value = float(morph["id_block"].get(morph["name"], 0.0))
        if morph["last_value"] is None or abs(value - morph["last_value"]) > 0.000001:
            changed = True
        morph["last_value"] = value
        if abs(value) <= 0.000001:
            continue

        pose_name = "positive" if value > 0.0 else "negative"
        pose = morph["poses"].get(pose_name)
        if not pose:
            continue
        source_value = pose.get("source_value", 1.0) or 1.0
        factor = abs(value / source_value)
        active += 1

        for bone_name, delta in pose["bone_deltas"].items():
            if bone_name not in matrices:
                continue
            matrices[bone_name] = matrices[bone_name] @ weighted_delta_matrix(delta, factor)

    if not changed:
        return 0, active

    for bone_name, matrix in matrices.items():
        runtime["neutral_bones"][bone_name]["pose_bone"].matrix_basis = matrix
    runtime["last_frame_key"] = current_frame_key
    armature["daz_mhx_v2_status"] = (
        f"Applied {active} active cached bone morphs to {len(matrices)} driver bones."
    )
    return len(matrices), active


def load_or_apply_runtime(armature, scene=None, force_reload=False, force_apply=False):
    runtime = runtime_cache_for_armature(armature, force_reload=force_reload)
    if not runtime:
        armature["daz_mhx_v2_status"] = "No V2 cache found for this armature."
        return 0, 0
    return apply_runtime_cache(
        armature,
        runtime,
        scene=scene,
        force=force_apply or force_reload,
    )


@persistent
def daz_mhx_v2_depsgraph_handler(scene, depsgraph):
    global _APPLYING
    if _APPLYING:
        return

    _APPLYING = True
    try:
        for obj in scene.objects:
            if obj.type != "ARMATURE":
                continue
            if not obj.get("daz_mhx_v2_cache_path"):
                continue
            load_or_apply_runtime(obj, scene=scene)
    finally:
        _APPLYING = False


class DAZMHX_OT_v2_aggressive_convert(bpy.types.Operator):
    bl_idname = "daz_mhx.v2_aggressive_convert"
    bl_label = "Aggressive Convert Bone Morphs"
    bl_description = "Bake bone-only custom props, delete their drivers/properties, rebuild controls, and load runtime cache"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        armature = selected_armature(context)
        if not armature:
            self.report({"WARNING"}, "Select an armature.")
            return {"CANCELLED"}

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        cache, classification = make_cache(context, armature)
        cache_path = cache_path_for_armature(armature)
        with open(cache_path, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, indent=2, sort_keys=True)

        removed_transform_drivers = delete_transform_drivers(
            armature,
            set(cache["affected_driver_bones"]),
        )
        removed_prop_drivers, deleted_props, rebuilt_props = delete_and_rebuild_props(
            armature,
            cache,
            classification,
        )

        armature["daz_mhx_v2_cache_path"] = cache_path
        armature["daz_mhx_v2_cache_id"] = armature.name
        armature["daz_mhx_v2_removed_transform_drivers"] = removed_transform_drivers
        armature["daz_mhx_v2_removed_prop_drivers"] = removed_prop_drivers
        armature["daz_mhx_v2_deleted_props"] = deleted_props
        armature["daz_mhx_v2_rebuilt_props"] = rebuilt_props

        runtime_cache_for_armature(armature, force_reload=True)
        load_or_apply_runtime(
            armature,
            scene=context.scene,
            force_reload=True,
            force_apply=True,
        )

        armature["daz_mhx_v2_status"] = (
            f"Converted {rebuilt_props} bone-only controls; removed "
            f"{removed_transform_drivers} transform drivers and "
            f"{removed_prop_drivers} custom-property drivers."
        )
        self.report(
            {"INFO"},
            (
                f"Wrote {cache_path}; converted {rebuilt_props} controls; "
                f"removed {removed_transform_drivers} transform drivers."
            ),
        )
        return {"FINISHED"}


class DAZMHX_OT_v2_load_runtime(bpy.types.Operator):
    bl_idname = "daz_mhx.v2_load_runtime"
    bl_label = "Load V2 Runtime Cache"
    bl_description = "Load the V2 JSON cache and apply current rebuilt control values"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        armature = selected_armature(context)
        if not armature:
            self.report({"WARNING"}, "Select an armature.")
            return {"CANCELLED"}

        armature["daz_mhx_v2_cache_path"] = armature.get(
            "daz_mhx_v2_cache_path",
            cache_path_for_armature(armature),
        )
        applied, active = load_or_apply_runtime(
            armature,
            scene=context.scene,
            force_reload=True,
            force_apply=True,
        )
        self.report({"INFO"}, f"Loaded V2 cache; applied {active} active morphs to {applied} bones.")
        return {"FINISHED"}


class DAZMHX_PT_v2_converter(bpy.types.Panel):
    bl_label = "Daz MHX V2 Converter"
    bl_idname = "DAZMHX_PT_v2_converter"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Daz MHX"

    def draw(self, context):
        layout = self.layout
        armature = selected_armature(context)

        layout.operator("daz_mhx.v2_aggressive_convert")
        layout.operator("daz_mhx.v2_load_runtime")
        if armature:
            layout.label(text=armature.get("daz_mhx_v2_status", "No V2 cache loaded."))
            layout.label(text=f"Cache: {os.path.basename(cache_path_for_armature(armature))}")
            runtime = runtime_cache_for_armature(armature)
            if runtime and runtime.get("morphs"):
                box = layout.box()
                box.label(text="Converted Bone Morphs")
                for morph in sorted(
                    runtime["morphs"],
                    key=lambda item: (
                        item.get("name", "").lower(),
                        item.get("scope", ""),
                    ),
                ):
                    id_block = morph.get("id_block")
                    name = morph.get("name")
                    if not id_block or not name or name not in id_block:
                        continue

                    label = name
                    if morph.get("scope") == "data":
                        label = f"{name} (data)"
                    row = box.row()
                    row.prop(id_block, id_prop_path(name), text=label, slider=True)
            elif armature.get("daz_mhx_v2_cache_path"):
                layout.label(text="No rebuilt morphs found in cache.")


classes = (
    DAZMHX_OT_v2_aggressive_convert,
    DAZMHX_OT_v2_load_runtime,
    DAZMHX_PT_v2_converter,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    if daz_mhx_v2_depsgraph_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(daz_mhx_v2_depsgraph_handler)


def unregister():
    if daz_mhx_v2_depsgraph_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(daz_mhx_v2_depsgraph_handler)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    _RUNTIME_CACHES.clear()


if __name__ == "__main__":
    register()
