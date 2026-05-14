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
    "description": "Convert MHX custom property morphs to cached driver-bone and shape-key values.",
    "category": "Object",
}


POSE_BONE_PATH_RE = re.compile(r'pose\.bones\["([^"]+)"\]\.(.+)')
CUSTOM_PROPERTY_PATH_RE = re.compile(r'\["([^"]+)"\]')
SAFE_CHANNELS = {"location", "rotation_euler", "rotation_quaternion", "scale"}

_RUNTIME_CACHES = {}
_APPLYING = False
_SUPPRESS_RNA_UPDATE = False
_RNA_CONTROL_PROPS = set()
_DIRTY_ARMATURES = set()
_DIRTY_TIMER_REGISTERED = False


@persistent
def clear_runtime_caches_on_undo(_scene):
    _RUNTIME_CACHES.clear()
    _DIRTY_ARMATURES.clear()


@persistent
def clear_runtime_caches_on_load(_dummy):
    _RUNTIME_CACHES.clear()
    _DIRTY_ARMATURES.clear()


def selected_armature(context):
    obj = context.object
    if obj and obj.type == "ARMATURE":
        return obj

    selected = [item for item in context.selected_objects if item.type == "ARMATURE"]
    return selected[0] if selected else None


def safe_filename(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "Armature"


def blend_output_dir():
    if bpy.data.filepath:
        return os.path.dirname(bpy.data.filepath)
    return bpy.path.abspath("//")


def blend_relative_path(path):
    try:
        return bpy.path.relpath(path)
    except ValueError:
        return path


def resolve_blend_path(path):
    return bpy.path.abspath(path) if path else path


def cache_path_for_armature(armature):
    return os.path.join(
        blend_output_dir(),
        f"morph_cache_{safe_filename(armature.name)}.json",
    )


def bone_driver_cleanup_path_for_armature(armature):
    return os.path.join(
        blend_output_dir(),
        f"daz_mhx_bone_driver_cleanup_{safe_filename(armature.name)}.json",
    )


def prop_driver_cleanup_path_for_armature(armature):
    return os.path.join(
        blend_output_dir(),
        f"daz_mhx_prop_driver_cleanup_{safe_filename(armature.name)}.json",
    )


def data_prop_cleanup_path_for_armature(armature):
    return os.path.join(
        blend_output_dir(),
        f"daz_mhx_data_prop_cleanup_{safe_filename(armature.name)}.json",
    )


def runtime_key_for_armature(armature, path):
    return (armature.as_pointer(), path)


def rna_safe_name(name):
    safe = re.sub(r"\W+", "_", name).strip("_")
    if not safe:
        safe = "morph"
    if safe[0].isdigit():
        safe = f"morph_{safe}"
    return safe


def rna_prop_name(scope, name, index):
    return f"daz_mhx_v2_{index:04d}_{scope}_{rna_safe_name(name)}"


def custom_properties_from_path(data_path):
    return CUSTOM_PROPERTY_PATH_RE.findall(data_path or "")


def shape_key_name_from_data_path(data_path):
    match = re.search(r'key_blocks\["([^"]+)"\]\.value', data_path or "")
    return match.group(1) if match else None


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


def shape_key_identifier(mesh, key_block):
    return f"{mesh.name}::{key_block.name}"


def shape_key_targets(armature):
    targets = {}
    for mesh in related_meshes(armature):
        shape_keys = mesh.data.shape_keys
        if not shape_keys:
            continue
        for key_block in shape_keys.key_blocks:
            if key_block.name == "Basis":
                continue
            key = shape_key_identifier(mesh, key_block)
            targets[key] = {
                "mesh": mesh.name,
                "shape_key": key_block.name,
                "value": key_block.value,
            }
    return targets


def snapshot_shape_keys(armature, target_keys=None):
    targets = shape_key_targets(armature)
    if target_keys is not None:
        targets = {
            key: value
            for key, value in targets.items()
            if key in target_keys
        }
    return {
        key: dict(value)
        for key, value in targets.items()
    }


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


def driver_bones_with_transform_drivers(armature):
    bones = set()
    if not armature.animation_data:
        return bones

    for fcurve in armature.animation_data.drivers:
        pose_path = parse_pose_bone_data_path(fcurve.data_path)
        if not pose_path:
            continue
        if not pose_path["is_driver_bone"]:
            continue
        if pose_path["channel_base"] not in SAFE_CHANNELS:
            continue
        bones.add(pose_path["bone_name"])
    return bones


def bone_transform_source_props(armature, driver_records):
    refs = set()
    transform_drivers = []
    for record in driver_records:
        pose_path = record["pose_path"]
        if not pose_path:
            continue
        if pose_path["channel_base"] not in SAFE_CHANNELS:
            continue
        if not pose_path["is_driver_bone"]:
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


def constraint_source_props(driver_records):
    refs = set()
    constraint_drivers = []
    for record in driver_records:
        pose_path = record["pose_path"]
        if not pose_path:
            continue
        if pose_path["channel_base"] != "constraints":
            continue

        refs.update(record["source_props"])
        constraint_drivers.append(
            {
                "data_path": record["data_path"],
                "array_index": record["array_index"],
                "source_props": sorted(record["source_props"]),
            }
        )
    return refs, constraint_drivers


def is_protected_prop_name(name):
    return name.startswith(("Mha", "pCTRL", "pJCM"))


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
    constraint_seeds, constraint_drivers = constraint_source_props(object_drivers)
    shape_touched = ancestor_props(shape_seeds, reverse_edges)
    bone_touched = ancestor_props(bone_seeds, reverse_edges)
    constraint_touched = ancestor_props(constraint_seeds, reverse_edges)

    controls = []
    internal_delete_props = []
    protected_props = []
    for key, prop_def in sorted(prop_defs.items()):
        reaches_bones = key in bone_touched
        reaches_shape_keys = key in shape_touched
        reaches_constraints = key in constraint_touched
        is_driven = key in driven_props
        is_protected = reaches_constraints or is_protected_prop_name(prop_def["name"])
        is_control = (
            (reaches_bones or reaches_shape_keys)
            and not is_driven
            and prop_def["is_numeric_scalar"]
            and not is_protected
        )

        entry = dict(prop_def)
        entry.update(
            {
                "key": key,
                "reaches_bones": reaches_bones,
                "reaches_shape_keys": reaches_shape_keys,
                "reaches_constraints": reaches_constraints,
                "is_protected": is_protected,
                "is_driven": is_driven,
            }
        )

        if is_control:
            controls.append(entry)
        elif is_protected:
            protected_props.append(entry)
        elif reaches_bones or reaches_shape_keys:
            internal_delete_props.append(entry)

    return {
        "prop_defs": prop_defs,
        "controls": controls,
        "internal_delete_props": internal_delete_props,
        "protected_props": protected_props,
        "bone_touched_props": sorted(bone_touched),
        "shape_touched_props": sorted(shape_touched),
        "constraint_touched_props": sorted(constraint_touched),
        "driven_props": sorted(driven_props),
        "prop_driver_records": all_prop_drivers,
        "shape_drivers": shape_drivers,
        "transform_drivers": transform_drivers,
        "constraint_drivers": constraint_drivers,
    }


def sorted_debug_prop_list(items):
    names = {item.get("name", "") for item in items if item.get("name")}
    return sorted(names, key=str.lower)


def debug_property_lists(classification):
    classified_props = classification["controls"] + classification["internal_delete_props"]
    protected_props = classification["protected_props"]
    bone_only = [
        item
        for item in classified_props
        if item.get("reaches_bones") and not item.get("reaches_shape_keys")
    ]
    mixed = [
        item
        for item in classified_props
        if item.get("reaches_bones") and item.get("reaches_shape_keys")
    ]
    shape_key_only = [
        item
        for item in classified_props
        if item.get("reaches_shape_keys") and not item.get("reaches_bones")
    ]

    return {
        "bone_morphs": sorted_debug_prop_list(bone_only),
        "mixed_morphs": sorted_debug_prop_list(mixed),
        "shapekey_morphs": sorted_debug_prop_list(shape_key_only),
        "protected_props": sorted_debug_prop_list(protected_props),
    }


def print_debug_property_lists(armature, classification, baked):
    debug_lists = debug_property_lists(classification)
    summary = {
        "controls": len(classification["controls"]),
        "baked_morphs": len(baked["morphs"]),
        "internal_delete_props": len(classification["internal_delete_props"]),
        "non_control_morph_props": len(classification["internal_delete_props"]),
        "bone_morph_props": len(debug_lists["bone_morphs"]),
        "mixed_morph_props": len(debug_lists["mixed_morphs"]),
        "shapekey_morph_props": len(debug_lists["shapekey_morphs"]),
        "protected_props": len(debug_lists["protected_props"]),
        "shape_key_drivers": len(classification["shape_drivers"]),
        "bone_transform_drivers": len(classification["transform_drivers"]),
        "constraint_drivers": len(classification["constraint_drivers"]),
    }

    print(f"\n[Daz MHX V2] Classification for {armature.name}")
    print("[Daz MHX V2] Summary:")
    for key, value in summary.items():
        print(f"  {key}: {value}")

    for label, names in (
        ("Bone morphs", debug_lists["bone_morphs"]),
        ("Mixed morphs", debug_lists["mixed_morphs"]),
        ("Shapekey morphs", debug_lists["shapekey_morphs"]),
        ("Protected props", debug_lists["protected_props"]),
    ):
        print(f"[Daz MHX V2] {label} ({len(names)}):")
        for name in names:
            print(f"  {name}")


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


def changed_shape_keys(neutral, posed, threshold=0.000001):
    changed = {}
    for key, snapshot in posed.items():
        neutral_snapshot = neutral.get(key)
        if not neutral_snapshot:
            continue
        if abs(snapshot["value"] - neutral_snapshot["value"]) <= threshold:
            continue
        changed[key] = snapshot
    return changed


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
    all_driver_bones = sorted(driver_bones_with_transform_drivers(armature))
    controls = classification["controls"]
    original_values = {
        control["key"]: get_prop_value(armature, control["key"])
        for control in controls
    }

    for control in controls:
        set_prop_value(armature, control["key"], 0.0)
    neutral = snapshot_driver_bones(context, armature, all_driver_bones)
    neutral_shape_keys_all = snapshot_shape_keys(armature)

    morphs = []
    affected_driver_bones = set()
    affected_shape_keys = set()
    for control in controls:
        key = control["key"]
        poses = {}
        for pose_name, direction in (("positive", 1), ("negative", -1)):
            sample_value = sample_value_for_control(control, direction)
            if sample_value is None:
                continue

            set_prop_value(armature, key, sample_value)
            posed = snapshot_driver_bones(context, armature, all_driver_bones)
            posed_shape_keys = snapshot_shape_keys(armature)
            changed_bones = {
                bone_name: snapshot
                for bone_name, snapshot in posed.items()
                if bone_name in neutral
                and max_matrix_diff(neutral[bone_name], snapshot) > 0.000001
            }
            changed_shapes = changed_shape_keys(neutral_shape_keys_all, posed_shape_keys)
            set_prop_value(armature, key, 0.0)
            if not changed_bones and not changed_shapes:
                continue

            affected_driver_bones.update(changed_bones)
            affected_shape_keys.update(changed_shapes)
            pose_record = {
                "source_value": sample_value,
            }
            if changed_bones:
                pose_record["driver_bases"] = changed_bones
            if changed_shapes:
                pose_record["shape_keys"] = changed_shapes
            poses[pose_name] = pose_record

        if poses:
            ui = control["ui"] if isinstance(control.get("ui"), dict) else {}
            morphs.append(
                {
                    "scope": control["scope"],
                    "name": control["name"],
                    "ui": ui,
                    "default": ui.get("default", 0.0),
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
        "neutral_shape_keys": {
            key: neutral_shape_keys_all[key]
            for key in sorted(affected_shape_keys)
            if key in neutral_shape_keys_all
        },
        "morphs": morphs,
    }


def cached_driver_bones(cache):
    bones = set((cache or {}).get("neutral_driver_bases", {}))
    for morph in (cache or {}).get("morphs", []):
        for pose in morph.get("poses", {}).values():
            bones.update(pose.get("driver_bases", {}))
    return bones


def compact_driver_audit_entry(armature, fcurve, pose_path, reason=None):
    entry = {
        "bone": pose_path["bone_name"],
        "channel": pose_path["channel_base"],
        "array_index": fcurve.array_index,
    }
    if fcurve.driver:
        expression = fcurve.driver.expression
        if expression:
            entry["expression"] = expression
        source_props = sorted(source_prop_refs_from_driver(armature, fcurve.driver))
        if source_props:
            entry["source_props"] = source_props
    if reason:
        entry["reason"] = reason
    return entry


def cleanup_cached_bone_transform_drivers(armature, cache):
    cached_bones = cached_driver_bones(cache)
    audit = {
        "armature": armature.name,
        "cached_driver_bone_count": len(cached_bones),
        "removed": [],
        "preserved": [],
        "ignored": [],
    }

    if not armature.animation_data:
        return audit

    for fcurve in list(armature.animation_data.drivers):
        pose_path = parse_pose_bone_data_path(fcurve.data_path)
        if not pose_path:
            continue

        if pose_path["channel_base"] not in SAFE_CHANNELS:
            audit["ignored"].append(
                compact_driver_audit_entry(
                    armature,
                    fcurve,
                    pose_path,
                    "not_transform_channel",
                )
            )
            continue

        if not pose_path["is_driver_bone"]:
            audit["ignored"].append(
                compact_driver_audit_entry(
                    armature,
                    fcurve,
                    pose_path,
                    "not_driver_bone",
                )
            )
            continue

        if pose_path["bone_name"] not in cached_bones:
            audit["preserved"].append(
                compact_driver_audit_entry(
                    armature,
                    fcurve,
                    pose_path,
                    "not_in_cache",
                )
            )
            continue

        audit["removed"].append(compact_driver_audit_entry(armature, fcurve, pose_path))
        armature.animation_data.drivers.remove(fcurve)

    return audit


def write_bone_driver_cleanup_audit(armature, audit):
    path = bone_driver_cleanup_path_for_armature(armature)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    summary = {
        "removed_driver_count": len(audit["removed"]),
        "preserved_driver_count": len(audit["preserved"]),
        "ignored_driver_count": len(audit["ignored"]),
        "cached_driver_bone_count": audit["cached_driver_bone_count"],
    }
    payload = {
        "schema_version": 1,
        "armature": audit["armature"],
        "summary": summary,
        "removed": sorted(
            audit["removed"],
            key=lambda item: (item["bone"], item["channel"], item["array_index"]),
        ),
        "preserved": sorted(
            audit["preserved"],
            key=lambda item: (item["bone"], item["channel"], item["array_index"]),
        ),
        "ignored": sorted(
            audit["ignored"],
            key=lambda item: (item["reason"], item["bone"], item["channel"], item["array_index"]),
        ),
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    return path, summary


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


def delete_safe_driver_bone_transform_drivers(armature):
    safe_driver_bones = set(copy_transform_links_by_driver_bone(armature))
    return delete_transform_drivers(armature, safe_driver_bones)


def delete_shape_key_drivers(armature):
    removed = 0
    for mesh in related_meshes(armature):
        shape_keys = mesh.data.shape_keys
        if not shape_keys or not shape_keys.animation_data:
            continue
        for fcurve in list(shape_keys.animation_data.drivers):
            shape_name = shape_key_name_from_data_path(fcurve.data_path)
            if shape_name and shape_name.startswith("pJCM"):
                continue
            shape_keys.animation_data.drivers.remove(fcurve)
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


def cached_prop_keys(cache):
    return {
        prop_key(morph["scope"], morph["name"])
        for morph in (cache or {}).get("morphs", [])
    }


def cached_prop_names(cache):
    return {
        morph["name"]
        for morph in (cache or {}).get("morphs", [])
    }


def compact_prop_driver_audit_entry(record, reason=None):
    output_prop = record.get("output_prop")
    scope = None
    name = None
    if output_prop:
        scope, name = split_prop_key(output_prop)

    entry = {
        "scope": scope or record.get("owner_scope"),
        "property": name,
        "array_index": record["array_index"],
    }
    fcurve = record.get("fcurve")
    if fcurve and fcurve.driver:
        expression = fcurve.driver.expression
        if expression:
            entry["expression"] = expression
    source_props = sorted(record.get("source_props", []))
    if source_props:
        entry["source_props"] = source_props
    if reason:
        entry["reason"] = reason
    return entry


def cleanup_cached_prop_drivers(armature, cache, classification):
    cache_keys = cached_prop_keys(cache)
    cache_names = cached_prop_names(cache)
    internal_keys = {item["key"] for item in classification["internal_delete_props"]}
    protected_keys = {item["key"] for item in classification["protected_props"]}
    constraint_keys = set(classification["constraint_touched_props"])
    deleted_record_ids = set()
    audit = {
        "armature": armature.name,
        "removed": [],
        "preserved": [],
        "ignored": [],
    }

    for record in classification["prop_driver_records"]:
        output_prop = record.get("output_prop")
        if not output_prop:
            audit["ignored"].append(
                compact_prop_driver_audit_entry(record, "not_custom_property_output")
            )
            continue

        scope, name = split_prop_key(output_prop)
        fcurve = record.get("fcurve")
        id_block = id_block_for_scope(armature, scope)
        if not fcurve or not id_block or not id_block.animation_data:
            audit["ignored"].append(compact_prop_driver_audit_entry(record, "missing_driver"))
            continue

        if output_prop in protected_keys or output_prop in constraint_keys or is_protected_prop_name(name):
            audit["preserved"].append(compact_prop_driver_audit_entry(record, "protected"))
            continue

        is_cached = output_prop in cache_keys
        is_internal = output_prop in internal_keys
        is_same_named_cached_prop = name in cache_names
        if not (is_cached or is_internal or is_same_named_cached_prop):
            audit["ignored"].append(compact_prop_driver_audit_entry(record, "not_cached_morph_prop"))
            continue

        deleted_record_ids.add(id(fcurve))
        audit["removed"].append(compact_prop_driver_audit_entry(record))
        id_block.animation_data.drivers.remove(fcurve)

    return audit, deleted_record_ids


def write_prop_driver_cleanup_audit(armature, audit):
    path = prop_driver_cleanup_path_for_armature(armature)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    summary = {
        "removed_driver_count": len(audit["removed"]),
        "preserved_driver_count": len(audit["preserved"]),
        "ignored_driver_count": len(audit["ignored"]),
    }
    payload = {
        "schema_version": 1,
        "armature": audit["armature"],
        "summary": summary,
        "removed": sorted(
            audit["removed"],
            key=lambda item: (item.get("scope") or "", item.get("property") or ""),
        ),
        "preserved": sorted(
            audit["preserved"],
            key=lambda item: (item.get("reason") or "", item.get("scope") or "", item.get("property") or ""),
        ),
        "ignored": sorted(
            audit["ignored"],
            key=lambda item: (item.get("reason") or "", item.get("scope") or "", item.get("property") or ""),
        ),
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    return path, summary


def compact_data_prop_driver_entry(armature, fcurve, name, reason=None):
    entry = {
        "property": name,
        "array_index": fcurve.array_index,
    }
    if fcurve.driver:
        expression = fcurve.driver.expression
        if expression:
            entry["expression"] = expression
        source_props = sorted(source_prop_refs_from_driver(armature, fcurve.driver))
        if source_props:
            entry["source_props"] = source_props
    if reason:
        entry["reason"] = reason
    return entry


def cleanup_driven_data_props(armature):
    audit = {
        "armature": armature.name,
        "removed": [],
        "preserved": [],
        "ignored": [],
        "deleted_props": [],
    }
    data = armature.data
    if not data or not data.animation_data:
        return audit

    props_to_delete = set()
    for fcurve in list(data.animation_data.drivers):
        names = custom_properties_from_path(fcurve.data_path)
        if not names:
            audit["ignored"].append(
                compact_data_prop_driver_entry(
                    armature,
                    fcurve,
                    None,
                    "not_custom_property_output",
                )
            )
            continue

        name = names[0]
        if "pCTRL" in name or name.startswith("pJCM"):
            audit["preserved"].append(
                compact_data_prop_driver_entry(
                    armature,
                    fcurve,
                    name,
                    "protected_pose_corrective",
                )
            )
            continue

        audit["removed"].append(compact_data_prop_driver_entry(armature, fcurve, name))
        data.animation_data.drivers.remove(fcurve)
        props_to_delete.add(name)

    for name in sorted(props_to_delete, key=str.lower):
        if name not in data:
            continue
        try:
            del data[name]
            audit["deleted_props"].append(name)
        except Exception:
            audit["ignored"].append(
                {
                    "property": name,
                    "reason": "delete_failed",
                }
            )

    return audit


def write_data_prop_cleanup_audit(armature, audit):
    path = data_prop_cleanup_path_for_armature(armature)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    summary = {
        "removed_driver_count": len(audit["removed"]),
        "deleted_prop_count": len(audit["deleted_props"]),
        "preserved_driver_count": len(audit["preserved"]),
        "ignored_driver_count": len(audit["ignored"]),
    }
    payload = {
        "schema_version": 1,
        "armature": audit["armature"],
        "summary": summary,
        "deleted_props": audit["deleted_props"],
        "removed": sorted(
            audit["removed"],
            key=lambda item: item.get("property") or "",
        ),
        "preserved": sorted(
            audit["preserved"],
            key=lambda item: item.get("property") or "",
        ),
        "ignored": sorted(
            audit["ignored"],
            key=lambda item: (item.get("reason") or "", item.get("property") or ""),
        ),
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    return path, summary


def delete_and_rebuild_props(armature, cache, classification):
    removed_prop_drivers = 0
    deleted_props = 0
    rebuild_controls = {
        prop_key(morph["scope"], morph["name"]): morph
        for morph in cache["morphs"]
        if not is_protected_prop_name(morph["name"])
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
        id_block = armature
        default = morph.get("default", 0.0)
        id_block[morph["name"]] = default if numeric_scalar(default) else 0.0
        update_property_ui(id_block, morph["name"], morph.get("ui", {}))

    return removed_prop_drivers, deleted_props, len(rebuild_controls)


def make_cache(context, armature):
    classification = build_classification(armature)
    baked = bake_controls(context, armature, classification)
    print_debug_property_lists(armature, classification, baked)
    return {
        "schema_version": 2,
        "kind": "daz_mhx_morph_cache",
        "armature": armature.name,
        "neutral_driver_bases": baked["neutral_driver_bases"],
        "neutral_shape_keys": baked["neutral_shape_keys"],
        "morphs": baked["morphs"],
    }, classification


def write_cache_file(armature, cache):
    cache_path = cache_path_for_armature(armature)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as handle:
        json.dump(cache, handle, indent=2, sort_keys=True)
    armature["daz_mhx_v2_cache_path"] = blend_relative_path(cache_path)
    armature["daz_mhx_v2_cache_id"] = armature.name
    return cache_path


def load_cache_file(armature):
    cache_path = resolve_blend_path(
        armature.get("daz_mhx_v2_cache_path", blend_relative_path(cache_path_for_armature(armature)))
    )
    if not os.path.exists(cache_path):
        return None, cache_path
    with open(cache_path, "r", encoding="utf-8") as handle:
        return json.load(handle), cache_path


def resolve_shape_key_runtime(snapshot):
    mesh = bpy.data.objects.get(snapshot.get("mesh", ""))
    if not mesh or mesh.type != "MESH" or not mesh.data.shape_keys:
        return None
    key_block = mesh.data.shape_keys.key_blocks.get(snapshot.get("shape_key", ""))
    if not key_block:
        return None
    return key_block


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


def shape_delta(neutral_snapshot, posed_snapshot):
    return posed_snapshot["value"] - neutral_snapshot["value"]


def runtime_cache_for_armature(armature, force_reload=False):
    path = resolve_blend_path(
        armature.get("daz_mhx_v2_cache_path", blend_relative_path(cache_path_for_armature(armature)))
    )
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

    neutral_shape_keys = {}
    for key, snapshot in data.get("neutral_shape_keys", {}).items():
        key_block = resolve_shape_key_runtime(snapshot)
        if not key_block:
            continue
        neutral_shape_keys[key] = {
            "key_block": key_block,
            "value": snapshot["value"],
            "snapshot": snapshot,
        }

    morphs = []
    morphs_by_rna = {}
    for index, morph in enumerate(data.get("morphs", [])):
        scope = morph.get("scope")
        name = morph.get("name")
        id_block = id_block_for_scope(armature, scope)
        if scope == "data" and (not id_block or name not in id_block) and name in armature:
            id_block = armature
        if not id_block or name not in id_block:
            continue

        prop_name = rna_prop_name(scope, name, index)
        label = name if scope == "object" else f"{name} (data)"
        current_value = id_block.get(name, morph.get("default", 0.0))
        register_rna_control_prop(prop_name, label, morph.get("ui", {}), current_value)

        runtime_morph = {
            "scope": scope,
            "name": name,
            "key": prop_key(scope, name),
            "id_block": id_block,
            "rna_prop_name": prop_name,
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
            shape_deltas = {}
            for key, posed_snapshot in pose.get("shape_keys", {}).items():
                neutral = neutral_shape_keys.get(key)
                if not neutral:
                    continue
                shape_deltas[key] = shape_delta(
                    neutral["snapshot"],
                    posed_snapshot,
                )
            runtime_morph["poses"][pose_name] = {
                "source_value": source_value,
                "bone_deltas": bone_deltas,
                "shape_deltas": shape_deltas,
            }
        morphs.append(runtime_morph)
        morphs_by_rna[prop_name] = runtime_morph

    runtime = {
        "path": path,
        "modified_time": modified_time,
        "data": data,
        "neutral_bones": neutral_bones,
        "neutral_shape_keys": neutral_shape_keys,
        "morphs": morphs,
        "morphs_by_rna": morphs_by_rna,
    }
    _RUNTIME_CACHES[cache_key] = runtime
    sync_rna_controls_from_id_props(armature, runtime)
    return runtime


def weighted_delta_matrix(delta, factor):
    factor = max(0.0, min(1.0, factor))
    loc = Vector((0.0, 0.0, 0.0)).lerp(delta["loc"], factor)
    rot = Quaternion().slerp(delta["rot"], factor)
    scale = Vector((1.0, 1.0, 1.0)).lerp(delta["scale"], factor)
    return Matrix.LocRotScale(loc, rot, scale)


def rna_prop_options_from_ui(ui):
    if not isinstance(ui, dict):
        ui = {}

    options = {}
    for source, target in (
        ("min", "min"),
        ("max", "max"),
        ("soft_min", "soft_min"),
        ("soft_max", "soft_max"),
        ("description", "description"),
        ("precision", "precision"),
        ("step", "step"),
    ):
        if source in ui and ui[source] is not None:
            options[target] = ui[source]
    return options


def make_rna_update_callback(prop_name):
    def update(self, context):
        if _SUPPRESS_RNA_UPDATE or _APPLYING:
            return
        if not self or self.type != "ARMATURE":
            return

        runtime = runtime_cache_for_armature(self)
        if not runtime:
            return

        morph = runtime.get("morphs_by_rna", {}).get(prop_name)
        if not morph:
            return

        value = float(getattr(self, prop_name))
        morph["id_block"][morph["name"]] = value
        mark_armature_dirty(self)

    return update


def register_rna_control_prop(prop_name, label, ui, default):
    if hasattr(bpy.types.Object, prop_name):
        _RNA_CONTROL_PROPS.add(prop_name)
        return

    kwargs = rna_prop_options_from_ui(ui)
    kwargs.update(
        {
            "name": label,
            "default": float(default) if numeric_scalar(default) else 0.0,
            "update": make_rna_update_callback(prop_name),
        }
    )
    setattr(bpy.types.Object, prop_name, bpy.props.FloatProperty(**kwargs))
    _RNA_CONTROL_PROPS.add(prop_name)


def sync_rna_controls_from_id_props(armature, runtime):
    global _SUPPRESS_RNA_UPDATE
    _SUPPRESS_RNA_UPDATE = True
    try:
        for morph in runtime.get("morphs", []):
            prop_name = morph.get("rna_prop_name")
            if not prop_name:
                continue
            value = float(morph["id_block"].get(morph["name"], 0.0))
            setattr(armature, prop_name, value)
            morph["last_value"] = value
    finally:
        _SUPPRESS_RNA_UPDATE = False


def flush_dirty_armatures():
    global _APPLYING, _DIRTY_TIMER_REGISTERED
    _DIRTY_TIMER_REGISTERED = False
    dirty = list(_DIRTY_ARMATURES)
    _DIRTY_ARMATURES.clear()
    if not dirty:
        return None

    _APPLYING = True
    try:
        for pointer in dirty:
            armature = None
            for obj in bpy.data.objects:
                if obj.type == "ARMATURE" and obj.as_pointer() == pointer:
                    armature = obj
                    break
            if not armature:
                continue

            runtime = runtime_cache_for_armature(armature)
            if not runtime:
                continue
            apply_runtime_cache(armature, runtime, force=True)
    finally:
        _APPLYING = False

    return None


def mark_armature_dirty(armature):
    global _DIRTY_TIMER_REGISTERED
    if not armature or armature.type != "ARMATURE":
        return
    _DIRTY_ARMATURES.add(armature.as_pointer())
    if _DIRTY_TIMER_REGISTERED:
        return
    _DIRTY_TIMER_REGISTERED = True
    bpy.app.timers.register(flush_dirty_armatures, first_interval=0.0)


def apply_runtime_cache(armature, runtime, force=False):
    changed = force
    active = 0
    matrices = None
    shape_values = None
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
        if not pose.get("bone_deltas") and not pose.get("shape_deltas"):
            continue
        source_value = pose.get("source_value", 1.0) or 1.0
        factor = abs(value / source_value)
        active += 1

        if pose.get("bone_deltas") and matrices is None:
            matrices = {
                bone_name: item["matrix"].copy()
                for bone_name, item in runtime["neutral_bones"].items()
            }
        if pose.get("shape_deltas") and shape_values is None:
            shape_values = {
                key: item["value"]
                for key, item in runtime["neutral_shape_keys"].items()
            }

        for bone_name, delta in pose.get("bone_deltas", {}).items():
            if bone_name not in matrices:
                continue
            matrices[bone_name] = matrices[bone_name] @ weighted_delta_matrix(delta, factor)
        for key, delta in pose.get("shape_deltas", {}).items():
            if key not in shape_values:
                continue
            shape_values[key] += delta * factor

    if not changed:
        return 0, active

    if active == 0 or matrices is None:
        matrices = {
            bone_name: item["matrix"].copy()
            for bone_name, item in runtime["neutral_bones"].items()
        }
    if active == 0 or shape_values is None:
        shape_values = {
            key: item["value"]
            for key, item in runtime["neutral_shape_keys"].items()
        }

    for bone_name, matrix in matrices.items():
        runtime["neutral_bones"][bone_name]["pose_bone"].matrix_basis = matrix
    for key, value in shape_values.items():
        runtime["neutral_shape_keys"][key]["key_block"].value = value
    armature["daz_mhx_v2_status"] = (
        f"Applied {active} active cached morphs to {len(matrices)} driver bones "
        f"and {len(shape_values)} shape keys."
    )
    return len(matrices) + len(shape_values), active


def load_or_apply_runtime(armature, force_reload=False, force_apply=False):
    runtime = runtime_cache_for_armature(armature, force_reload=force_reload)
    if not runtime:
        armature["daz_mhx_v2_status"] = "No V2 cache found for this armature."
        return 0, 0
    return apply_runtime_cache(
        armature,
        runtime,
        force=force_apply or force_reload,
    )


def reset_runtime_controls(armature):
    runtime = runtime_cache_for_armature(armature)
    if not runtime:
        armature["daz_mhx_v2_status"] = "No V2 cache found for this armature."
        return 0, 0

    global _SUPPRESS_RNA_UPDATE
    _SUPPRESS_RNA_UPDATE = True
    try:
        reset_count = 0
        for morph in runtime.get("morphs", []):
            default = morph.get("default", 0.0)
            if not numeric_scalar(default):
                default = 0.0
            value = float(default)
            morph["id_block"][morph["name"]] = value
            prop_name = morph.get("rna_prop_name")
            if prop_name and hasattr(armature, prop_name):
                setattr(armature, prop_name, value)
            morph["last_value"] = None
            reset_count += 1
    finally:
        _SUPPRESS_RNA_UPDATE = False

    applied, _active = apply_runtime_cache(armature, runtime, force=True)
    armature["daz_mhx_v2_status"] = f"Reset {reset_count} converted morphs to defaults."
    return applied, reset_count


class DAZMHX_OT_v2_write_cache(bpy.types.Operator):
    bl_idname = "daz_mhx.v2_write_cache"
    bl_label = "Write Morph Cache"
    bl_description = "Bake custom prop morphs to JSON without deleting drivers or custom properties"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        armature = selected_armature(context)
        if not armature:
            self.report({"WARNING"}, "Select an armature.")
            return {"CANCELLED"}

        cache, classification = make_cache(context, armature)
        cache_path = write_cache_file(armature, cache)
        runtime_cache_for_armature(armature, force_reload=True)

        armature["daz_mhx_v2_status"] = (
            f"Wrote {len(cache['morphs'])} cached morphs. "
            "Original drivers/custom props were not deleted."
        )
        self.report(
            {"INFO"},
            (
                f"Wrote {cache_path} with {len(cache['morphs'])} cached morphs."
            ),
        )
        return {"FINISHED"}


class DAZMHX_OT_v2_delete_cached_bone_drivers(bpy.types.Operator):
    bl_idname = "daz_mhx.v2_delete_cached_bone_drivers"
    bl_label = "Delete Cached Bone Drivers"
    bl_description = "Delete only drv-bone transform drivers covered by the existing V2 morph cache and write a sparse audit JSON"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        armature = selected_armature(context)
        if not armature:
            self.report({"WARNING"}, "Select an armature.")
            return {"CANCELLED"}

        cache, cache_path = load_cache_file(armature)
        if not cache:
            self.report({"WARNING"}, f"No V2 cache found at {cache_path}. Write the cache first.")
            return {"CANCELLED"}

        audit = cleanup_cached_bone_transform_drivers(armature, cache)
        audit_path, summary = write_bone_driver_cleanup_audit(armature, audit)
        armature["daz_mhx_v2_cache_path"] = cache_path
        armature["daz_mhx_v2_bone_driver_cleanup_path"] = blend_relative_path(audit_path)
        armature["daz_mhx_v2_removed_transform_drivers"] = summary["removed_driver_count"]
        armature["daz_mhx_v2_preserved_transform_drivers"] = summary["preserved_driver_count"]
        armature["daz_mhx_v2_ignored_transform_drivers"] = summary["ignored_driver_count"]

        runtime_cache_for_armature(armature, force_reload=True)
        load_or_apply_runtime(
            armature,
            force_reload=True,
            force_apply=True,
        )

        armature["daz_mhx_v2_status"] = (
            f"Removed {summary['removed_driver_count']} cached bone drivers; "
            f"preserved {summary['preserved_driver_count']}, ignored {summary['ignored_driver_count']}. "
            f"Audit: {os.path.basename(audit_path)}"
        )
        self.report(
            {"INFO"},
            (
                f"Removed {summary['removed_driver_count']} cached bone drivers. "
                f"Wrote {audit_path}."
            ),
        )
        return {"FINISHED"}


class DAZMHX_OT_v2_delete_cached_prop_drivers(bpy.types.Operator):
    bl_idname = "daz_mhx.v2_delete_cached_prop_drivers"
    bl_label = "Delete Cached Prop Drivers"
    bl_description = "Delete custom-property drivers covered by the existing V2 morph cache and write a sparse audit JSON"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        armature = selected_armature(context)
        if not armature:
            self.report({"WARNING"}, "Select an armature.")
            return {"CANCELLED"}

        cache, cache_path = load_cache_file(armature)
        if not cache:
            self.report({"WARNING"}, f"No V2 cache found at {cache_path}. Write the cache first.")
            return {"CANCELLED"}

        classification = build_classification(armature)
        audit, _deleted_record_ids = cleanup_cached_prop_drivers(armature, cache, classification)
        audit_path, summary = write_prop_driver_cleanup_audit(armature, audit)
        armature["daz_mhx_v2_cache_path"] = cache_path
        armature["daz_mhx_v2_prop_driver_cleanup_path"] = blend_relative_path(audit_path)
        armature["daz_mhx_v2_removed_prop_drivers"] = summary["removed_driver_count"]
        armature["daz_mhx_v2_preserved_prop_drivers"] = summary["preserved_driver_count"]
        armature["daz_mhx_v2_ignored_prop_drivers"] = summary["ignored_driver_count"]

        armature["daz_mhx_v2_status"] = (
            f"Removed {summary['removed_driver_count']} cached prop drivers; "
            f"preserved {summary['preserved_driver_count']}, ignored {summary['ignored_driver_count']}. "
            f"Audit: {os.path.basename(audit_path)}"
        )
        self.report(
            {"INFO"},
            (
                f"Removed {summary['removed_driver_count']} cached prop drivers. "
                f"Wrote {audit_path}."
            ),
        )
        return {"FINISHED"}


class DAZMHX_OT_v2_delete_driven_data_props(bpy.types.Operator):
    bl_idname = "daz_mhx.v2_delete_driven_data_props"
    bl_label = "Delete Driven Data Props"
    bl_description = "Delete every armature-data custom property that has a driver, except properties containing pCTRL"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        armature = selected_armature(context)
        if not armature:
            self.report({"WARNING"}, "Select an armature.")
            return {"CANCELLED"}

        audit = cleanup_driven_data_props(armature)
        audit_path, summary = write_data_prop_cleanup_audit(armature, audit)
        armature["daz_mhx_v2_data_prop_cleanup_path"] = blend_relative_path(audit_path)
        armature["daz_mhx_v2_removed_data_prop_drivers"] = summary["removed_driver_count"]
        armature["daz_mhx_v2_deleted_data_props"] = summary["deleted_prop_count"]
        armature["daz_mhx_v2_preserved_data_prop_drivers"] = summary["preserved_driver_count"]

        armature["daz_mhx_v2_status"] = (
            f"Deleted {summary['deleted_prop_count']} driven data props; "
            f"removed {summary['removed_driver_count']} data prop drivers, "
            f"preserved {summary['preserved_driver_count']} pCTRL drivers. "
            f"Audit: {os.path.basename(audit_path)}"
        )
        self.report(
            {"INFO"},
            (
                f"Deleted {summary['deleted_prop_count']} driven data props. "
                f"Wrote {audit_path}."
            ),
        )
        return {"FINISHED"}


class DAZMHX_OT_v2_write_cache_and_clean(bpy.types.Operator):
    bl_idname = "daz_mhx.v2_write_cache_and_clean"
    bl_label = "Write Cache and Clean Rig"
    bl_description = "Write a fresh V2 morph cache, delete converted drivers and props, then rebuild cached controls"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        armature = selected_armature(context)
        if not armature:
            self.report({"WARNING"}, "Select an armature.")
            return {"CANCELLED"}

        cache, classification = make_cache(context, armature)
        cache_path = write_cache_file(armature, cache)
        classification = build_classification(armature)
        audit = cleanup_cached_bone_transform_drivers(armature, cache)
        audit_path, audit_summary = write_bone_driver_cleanup_audit(armature, audit)
        removed_transform_drivers = audit_summary["removed_driver_count"]
        prop_audit, _deleted_record_ids = cleanup_cached_prop_drivers(
            armature,
            cache,
            classification,
        )
        prop_audit_path, prop_audit_summary = write_prop_driver_cleanup_audit(
            armature,
            prop_audit,
        )
        data_prop_audit = cleanup_driven_data_props(armature)
        data_prop_audit_path, data_prop_summary = write_data_prop_cleanup_audit(
            armature,
            data_prop_audit,
        )
        removed_shape_key_drivers = delete_shape_key_drivers(armature)
        removed_prop_drivers, deleted_props, rebuilt_props = delete_and_rebuild_props(
            armature,
            cache,
            classification,
        )
        removed_prop_drivers += prop_audit_summary["removed_driver_count"]
        removed_prop_drivers += data_prop_summary["removed_driver_count"]
        deleted_props += data_prop_summary["deleted_prop_count"]

        armature["daz_mhx_v2_cache_path"] = cache_path
        armature["daz_mhx_v2_cache_id"] = armature.name
        armature["daz_mhx_v2_removed_transform_drivers"] = removed_transform_drivers
        armature["daz_mhx_v2_preserved_transform_drivers"] = audit_summary["preserved_driver_count"]
        armature["daz_mhx_v2_ignored_transform_drivers"] = audit_summary["ignored_driver_count"]
        armature["daz_mhx_v2_bone_driver_cleanup_path"] = blend_relative_path(audit_path)
        armature["daz_mhx_v2_removed_shape_key_drivers"] = removed_shape_key_drivers
        armature["daz_mhx_v2_removed_prop_drivers"] = removed_prop_drivers
        armature["daz_mhx_v2_preserved_prop_drivers"] = prop_audit_summary["preserved_driver_count"]
        armature["daz_mhx_v2_ignored_prop_drivers"] = prop_audit_summary["ignored_driver_count"]
        armature["daz_mhx_v2_prop_driver_cleanup_path"] = blend_relative_path(prop_audit_path)
        armature["daz_mhx_v2_data_prop_cleanup_path"] = blend_relative_path(data_prop_audit_path)
        armature["daz_mhx_v2_removed_data_prop_drivers"] = data_prop_summary["removed_driver_count"]
        armature["daz_mhx_v2_deleted_data_props"] = data_prop_summary["deleted_prop_count"]
        armature["daz_mhx_v2_preserved_data_prop_drivers"] = data_prop_summary["preserved_driver_count"]
        armature["daz_mhx_v2_deleted_props"] = deleted_props
        armature["daz_mhx_v2_rebuilt_props"] = rebuilt_props

        runtime_cache_for_armature(armature, force_reload=True)
        load_or_apply_runtime(
            armature,
            force_reload=True,
            force_apply=True,
        )

        armature["daz_mhx_v2_status"] = (
            f"Wrote cache and rebuilt {rebuilt_props} cached controls; removed "
            f"{removed_transform_drivers} transform drivers and "
            f"{removed_shape_key_drivers} shape-key drivers and "
            f"{removed_prop_drivers} custom-property drivers. "
            f"Bone audit: {os.path.basename(audit_path)}; "
            f"prop audit: {os.path.basename(prop_audit_path)}; "
            f"data audit: {os.path.basename(data_prop_audit_path)}"
        )
        self.report(
            {"INFO"},
            (
                f"Wrote cache and rebuilt {rebuilt_props} controls; removed "
                f"{removed_transform_drivers} transform drivers and "
                f"{removed_shape_key_drivers} shape-key drivers."
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
            blend_relative_path(cache_path_for_armature(armature)),
        )
        applied, active = load_or_apply_runtime(
            armature,
            force_reload=True,
            force_apply=True,
        )
        self.report({"INFO"}, f"Loaded V2 cache; applied {active} active morphs to {applied} bones.")
        return {"FINISHED"}


class DAZMHX_OT_v2_reset_all(bpy.types.Operator):
    bl_idname = "daz_mhx.v2_reset_all"
    bl_label = "Reset All"
    bl_description = "Reset all rebuilt V2 morph controls to their default values"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        armature = selected_armature(context)
        if not armature:
            self.report({"WARNING"}, "Select an armature.")
            return {"CANCELLED"}

        applied, reset_count = reset_runtime_controls(armature)
        self.report({"INFO"}, f"Reset {reset_count} morphs; applied defaults to {applied} bones.")
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

        layout.operator("daz_mhx.v2_write_cache_and_clean")
        layout.operator("daz_mhx.v2_reset_all")
        if armature:
            layout.label(text=armature.get("daz_mhx_v2_status", "No V2 cache loaded."))
            layout.label(text=f"Cache: {os.path.basename(cache_path_for_armature(armature))}")
            runtime = runtime_cache_for_armature(armature)
            if runtime and runtime.get("morphs"):
                box = layout.box()
                box.label(text="Converted Morphs")
                for morph in sorted(
                    runtime["morphs"],
                    key=lambda item: (
                        item.get("name", "").lower(),
                        item.get("scope", ""),
                    ),
                ):
                    prop_name = morph.get("rna_prop_name")
                    name = morph.get("name")
                    if not prop_name or not name or not hasattr(armature, prop_name):
                        continue

                    label = name
                    if morph.get("scope") == "data":
                        label = f"{name} (data)"
                    row = box.row()
                    row.prop(armature, prop_name, text=label, slider=True)
            elif armature.get("daz_mhx_v2_cache_path"):
                layout.label(text="No rebuilt morphs found in cache.")


classes = (
    DAZMHX_OT_v2_write_cache,
    DAZMHX_OT_v2_delete_cached_bone_drivers,
    DAZMHX_OT_v2_delete_cached_prop_drivers,
    DAZMHX_OT_v2_delete_driven_data_props,
    DAZMHX_OT_v2_write_cache_and_clean,
    DAZMHX_OT_v2_load_runtime,
    DAZMHX_OT_v2_reset_all,
    DAZMHX_PT_v2_converter,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    if clear_runtime_caches_on_undo not in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.append(clear_runtime_caches_on_undo)
    if clear_runtime_caches_on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(clear_runtime_caches_on_load)


def unregister():
    global _DIRTY_TIMER_REGISTERED
    if clear_runtime_caches_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(clear_runtime_caches_on_load)
    if clear_runtime_caches_on_undo in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.remove(clear_runtime_caches_on_undo)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    for prop_name in list(_RNA_CONTROL_PROPS):
        if hasattr(bpy.types.Object, prop_name):
            delattr(bpy.types.Object, prop_name)
    _RNA_CONTROL_PROPS.clear()
    _RUNTIME_CACHES.clear()
    _DIRTY_ARMATURES.clear()
    _DIRTY_TIMER_REGISTERED = False


if __name__ == "__main__":
    register()
