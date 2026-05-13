import json
import os
import re

import bpy
from mathutils import Matrix


bl_info = {
    "name": "Daz MHX Test Converter",
    "author": "Codex",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Daz MHX",
    "description": "Test-cache one MHX custom prop driver chain to JSON and apply it to driver bones with an update callback.",
    "category": "Object",
}


OUTPUT_DIR = r"C:\Users\meat\Documents\blender\code\Daz Cleaner"
DEFAULT_PROP_NAME = "eCTRLSerious"
TEST_CONTROL_NAME = "serious"
DEFAULT_OUTPUT_FILENAME = "daz_mhx_test_eCTRLSerious.json"

POSE_BONE_PATH_RE = re.compile(r'pose\.bones\["([^"]+)"\]\.(.+)')
CUSTOM_PROPERTY_PATH_RE = re.compile(r'\["([^"]+)"\]')

_JSON_CACHE = {}
_RUNTIME_CACHE = {}


def output_path():
    return os.path.join(OUTPUT_DIR, DEFAULT_OUTPUT_FILENAME)


def cache_id_for_armature(armature):
    return armature.name if armature else "UNKNOWN_ARMATURE"


def selected_armature(context):
    obj = context.object
    if obj and obj.type == "ARMATURE":
        return obj

    selected = [item for item in context.selected_objects if item.type == "ARMATURE"]
    return selected[0] if selected else None


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


def driver_uses_custom_prop(fcurve, prop_name):
    for variable in fcurve.driver.variables:
        for target in variable.targets:
            if prop_name in custom_properties_from_path(getattr(target, "data_path", "")):
                return True
    return False


def is_pose_transform_driver_for_prop(fcurve, prop_name):
    pose_path = parse_pose_bone_data_path(fcurve.data_path)
    if not pose_path:
        return False

    if pose_path["channel_base"] not in {
        "location",
        "rotation_euler",
        "rotation_quaternion",
        "scale",
    }:
        return False

    return driver_uses_custom_prop(fcurve, prop_name)


def copy_transform_links_by_driver_bone(armature):
    links = {}
    for pose_bone in armature.pose.bones:
        for index, constraint in enumerate(pose_bone.constraints):
            if constraint.type != "COPY_TRANSFORMS":
                continue

            target = getattr(constraint, "target", None)
            target_bone = getattr(constraint, "subtarget", "")
            safe_shape = (
                target == armature
                and "(drv)" in target_bone
                and constraint.owner_space == "LOCAL"
                and constraint.target_space == "LOCAL"
                and abs(constraint.influence - 1.0) < 0.0001
                and not constraint.mute
            )
            if not safe_shape:
                continue

            links.setdefault(target_bone, []).append(
                {
                    "author_bone": pose_bone.name,
                    "constraint_index": index,
                    "constraint_name": constraint.name,
                    "owner_space": constraint.owner_space,
                    "target_space": constraint.target_space,
                    "influence": constraint.influence,
                }
            )
    return links


def matching_driver_records(armature, prop_name):
    records = []
    if not armature.animation_data:
        return records

    links_by_driver_bone = copy_transform_links_by_driver_bone(armature)
    for fcurve in armature.animation_data.drivers:
        if not is_pose_transform_driver_for_prop(fcurve, prop_name):
            continue

        pose_path = parse_pose_bone_data_path(fcurve.data_path)
        driver_bone = pose_path["bone_name"]
        author_links = links_by_driver_bone.get(driver_bone, [])
        if not author_links:
            continue

        records.append(
            {
                "data_path": fcurve.data_path,
                "array_index": fcurve.array_index,
                "expression": fcurve.driver.expression,
                "driver_bone": driver_bone,
                "driven_channel": pose_path["channel_base"],
                "author_links": author_links,
            }
        )

    return records


def safe_driver_bone_records(armature):
    return [
        {
            "data_path": "",
            "array_index": -1,
            "expression": "",
            "driver_bone": driver_bone,
            "driven_channel": "matrix_basis",
            "author_links": author_links,
            "record_type": "evaluated_changed_driver_bone",
        }
        for driver_bone, author_links in copy_transform_links_by_driver_bone(armature).items()
    ]


def transform_snapshot(pose_bone):
    return {
        "rotation_mode": pose_bone.rotation_mode,
        "location": list(pose_bone.location),
        "rotation_euler": list(pose_bone.rotation_euler),
        "rotation_quaternion": list(pose_bone.rotation_quaternion),
        "rotation_axis_angle": list(pose_bone.rotation_axis_angle),
        "scale": list(pose_bone.scale),
        "matrix_basis": [list(row) for row in pose_bone.matrix_basis],
    }


def restore_transform_snapshot(pose_bone, snapshot):
    pose_bone.location = snapshot["location"]
    pose_bone.rotation_mode = snapshot["rotation_mode"]
    pose_bone.rotation_euler = snapshot["rotation_euler"]
    pose_bone.rotation_quaternion = snapshot["rotation_quaternion"]
    pose_bone.rotation_axis_angle = snapshot["rotation_axis_angle"]
    pose_bone.scale = snapshot["scale"]
    pose_bone.matrix_basis = Matrix(snapshot["matrix_basis"])


def matrix_to_plain(matrix):
    return [list(row) for row in matrix]


def matrix_from_plain(rows):
    return Matrix(rows)


def world_matrix_for_pose_bone(armature, pose_bone):
    return armature.matrix_world @ pose_bone.matrix


def pose_matrix_from_world_matrix(armature, world_matrix):
    return armature.matrix_world.inverted() @ world_matrix


def force_depsgraph_update(context):
    for obj in context.selected_objects:
        obj.update_tag()
    context.view_layer.update()
    context.evaluated_depsgraph_get().update()


def safe_fcurve_evaluate(fcurve):
    try:
        return fcurve.evaluate(bpy.context.scene.frame_current)
    except Exception as error:
        return f"ERROR: {error}"


def max_matrix_basis_diff(a, b):
    a_values = [value for row in a["matrix_basis"] for value in row]
    b_values = [value for row in b["matrix_basis"] for value in row]
    return max(abs(x - y) for x, y in zip(a_values, b_values))


def filter_changed_driver_records(records, neutral_bases, posed_bases, threshold=0.000001):
    changed = []
    for record in records:
        driver_bone = record["driver_bone"]
        neutral = neutral_bases.get(driver_bone)
        posed = posed_bases.get(driver_bone)
        if not neutral or not posed:
            continue
        if max_matrix_basis_diff(neutral, posed) > threshold:
            changed.append(record)
    return changed


def debug_driver_sources(fcurve):
    sources = []
    for variable in fcurve.driver.variables:
        for target in variable.targets:
            id_block = getattr(target, "id", None)
            data_path = getattr(target, "data_path", "")
            value = None
            if id_block and data_path:
                try:
                    value = id_block.path_resolve(data_path)
                except Exception as error:
                    value = f"ERROR: {error}"

            sources.append(
                {
                    "variable_name": variable.name,
                    "variable_type": variable.type,
                    "target_id": getattr(id_block, "name", None),
                    "data_path": data_path,
                    "value": value,
                    "bone_target": getattr(target, "bone_target", ""),
                    "transform_type": getattr(target, "transform_type", ""),
                    "transform_space": getattr(target, "transform_space", ""),
                }
            )
    return sources


def debug_snapshot(context, armature, prop_name, records, label):
    depsgraph = context.evaluated_depsgraph_get()
    evaluated_armature = armature.evaluated_get(depsgraph)
    driver_bones = sorted({record["driver_bone"] for record in records})
    author_bones = author_bone_names_from_records(records)
    fcurves_by_key = {}

    if armature.animation_data:
        for fcurve in armature.animation_data.drivers:
            fcurves_by_key[(fcurve.data_path, fcurve.array_index)] = fcurve

    driver_info = []
    for record in records:
        fcurve = fcurves_by_key.get((record["data_path"], record["array_index"]))
        driver_info.append(
            {
                "data_path": record["data_path"],
                "array_index": record["array_index"],
                "driver_bone": record["driver_bone"],
                "expression": record["expression"],
                "fcurve_value": safe_fcurve_evaluate(fcurve) if fcurve else None,
                "sources": debug_driver_sources(fcurve) if fcurve else [],
            }
        )

    return {
        "label": label,
        "source_property": prop_name,
        "source_property_value": armature.get(prop_name, None),
        "scene_frame": context.scene.frame_current,
        "driver_info": driver_info,
        "original_driver_bases": {
            bone_name: transform_snapshot(armature.pose.bones[bone_name])
            for bone_name in driver_bones
        },
        "evaluated_driver_bases": {
            bone_name: transform_snapshot(evaluated_armature.pose.bones[bone_name])
            for bone_name in driver_bones
        },
        "evaluated_author_world_matrices": {
            bone_name: matrix_to_plain(
                world_matrix_for_pose_bone(
                    evaluated_armature,
                    evaluated_armature.pose.bones[bone_name],
                )
            )
            for bone_name in author_bones
        },
    }


def author_bone_names_from_records(records):
    return sorted(
        {
            link["author_bone"]
            for record in records
            for link in record["author_links"]
        }
    )


def constraint_keys_from_records(records):
    return sorted(
        {
            (link["author_bone"], link["constraint_index"])
            for record in records
            for link in record["author_links"]
        }
    )


def mute_constraints_for_records(armature, records, mute=True):
    original_mutes = {}
    for bone_name, constraint_index in constraint_keys_from_records(records):
        pose_bone = armature.pose.bones.get(bone_name)
        if not pose_bone or constraint_index >= len(pose_bone.constraints):
            continue

        constraint = pose_bone.constraints[constraint_index]
        original_mutes[(bone_name, constraint_index)] = constraint.mute
        constraint.mute = mute

    return original_mutes


def restore_constraint_mutes(armature, original_mutes):
    for (bone_name, constraint_index), mute in original_mutes.items():
        pose_bone = armature.pose.bones.get(bone_name)
        if not pose_bone or constraint_index >= len(pose_bone.constraints):
            continue

        pose_bone.constraints[constraint_index].mute = mute


def bone_depth(pose_bone):
    depth = 0
    parent = pose_bone.parent
    while parent:
        depth += 1
        parent = parent.parent
    return depth


def author_bone_names_parent_first(armature, bone_names):
    return sorted(
        bone_names,
        key=lambda name: bone_depth(armature.pose.bones[name])
        if name in armature.pose.bones
        else 0,
    )


def convert_author_world_matrices_to_basis(context, armature, records, world_matrices):
    author_bones = author_bone_names_parent_first(
        armature,
        list(world_matrices.keys()),
    )
    original_author_transforms = {
        bone_name: transform_snapshot(armature.pose.bones[bone_name])
        for bone_name in author_bones
    }

    original_mutes = mute_constraints_for_records(armature, records, mute=True)
    force_depsgraph_update(context)

    for bone_name in author_bones:
        pose_bone = armature.pose.bones.get(bone_name)
        if pose_bone:
            world_matrix = matrix_from_plain(world_matrices[bone_name])
            pose_bone.matrix = pose_matrix_from_world_matrix(armature, world_matrix)

    force_depsgraph_update(context)

    converted_basis = {
        bone_name: transform_snapshot(armature.pose.bones[bone_name])
        for bone_name in author_bones
    }

    for bone_name, snapshot in original_author_transforms.items():
        restore_transform_snapshot(armature.pose.bones[bone_name], snapshot)
    restore_constraint_mutes(armature, original_mutes)
    force_depsgraph_update(context)

    return original_author_transforms, converted_basis


def bake_prop_to_json(context, armature, prop_name, prop_value=1.0):
    direct_records = matching_driver_records(armature, prop_name)
    all_safe_records = safe_driver_bone_records(armature)
    if not direct_records:
        raise RuntimeError(f"No safe pose-bone transform drivers found for {prop_name!r}.")

    all_safe_driver_bones = sorted({record["driver_bone"] for record in all_safe_records})
    original_prop_value = armature.get(prop_name, 0.0)
    force_depsgraph_update(context)
    depsgraph = context.evaluated_depsgraph_get()
    evaluated_armature = armature.evaluated_get(depsgraph)
    neutral_driver_bases = {
        bone_name: transform_snapshot(evaluated_armature.pose.bones[bone_name])
        for bone_name in all_safe_driver_bones
    }
    debug_before = debug_snapshot(context, armature, prop_name, direct_records, "before")

    armature[prop_name] = prop_value
    force_depsgraph_update(context)
    force_depsgraph_update(context)
    debug_after = debug_snapshot(context, armature, prop_name, direct_records, "after")

    depsgraph = context.evaluated_depsgraph_get()
    evaluated_armature = armature.evaluated_get(depsgraph)
    posed_driver_bases_all = {
        bone_name: transform_snapshot(evaluated_armature.pose.bones[bone_name])
        for bone_name in all_safe_driver_bones
    }
    records = filter_changed_driver_records(
        all_safe_records,
        neutral_driver_bases,
        posed_driver_bases_all,
    )
    driver_bones = sorted({record["driver_bone"] for record in records})
    author_bones = author_bone_names_from_records(records)
    evaluated_author_world_matrices = {
        bone_name: matrix_to_plain(
            world_matrix_for_pose_bone(
                evaluated_armature,
                evaluated_armature.pose.bones[bone_name],
            )
        )
        for bone_name in author_bones
    }
    posed_driver_bases = {
        bone_name: posed_driver_bases_all[bone_name]
        for bone_name in driver_bones
    }
    neutral_driver_bases = {
        bone_name: neutral_driver_bases[bone_name]
        for bone_name in driver_bones
    }

    armature[prop_name] = original_prop_value
    force_depsgraph_update(context)
    debug_restored = debug_snapshot(context, armature, prop_name, direct_records, "restored")

    neutral_author_bases, baked_author_bases = convert_author_world_matrices_to_basis(
        context,
        armature,
        records,
        evaluated_author_world_matrices,
    )

    return {
        "schema_version": 1,
        "kind": "daz_mhx_test_driver_pose_cache",
        "armature": armature.name,
        "cache_id": cache_id_for_armature(armature),
        "source_property": prop_name,
        "source_value": prop_value,
        "direct_driver_record_count": len(direct_records),
        "driver_record_count": len(records),
        "author_bone_count": len(author_bones),
        "driver_bone_count": len(driver_bones),
        "driver_records": records,
        "direct_driver_records": direct_records,
        "poses": {
            "positive": {
                "source_value": prop_value,
                "evaluated_author_world_matrices": evaluated_author_world_matrices,
                "neutral_author_bases": neutral_author_bases,
                "author_bases": baked_author_bases,
                "neutral_driver_bases": neutral_driver_bases,
                "driver_bases": posed_driver_bases,
            }
        },
        "debug": {
            "before": debug_before,
            "after": debug_after,
            "restored": debug_restored,
            "driver_basis_diffs": {
                bone_name: max_matrix_basis_diff(
                    neutral_driver_bases[bone_name],
                    posed_driver_bases[bone_name],
                )
                for bone_name in driver_bones
            },
        },
        "notes": [
            "This test cache captures every safe driver bone whose evaluated transform changes after setting one custom property.",
            "It does not rely only on direct fcurves that mention the source property, so intermediate custom-property chains are included.",
            "The RNA test property named serious applies cached driver-bone basis transforms through an update callback.",
        ],
    }


def lerp_sequence(a, b, weight):
    return [x + ((y - x) * weight) for x, y in zip(a, b)]


def lerp_matrix_basis(neutral_rows, posed_rows, weight):
    neutral = matrix_from_plain(neutral_rows)
    posed = matrix_from_plain(posed_rows)
    neutral_loc, neutral_rot, neutral_scale = neutral.decompose()
    posed_loc, posed_rot, posed_scale = posed.decompose()

    loc = neutral_loc.lerp(posed_loc, weight)
    rot = neutral_rot.slerp(posed_rot, weight)
    scale = neutral_scale.lerp(posed_scale, weight)
    return Matrix.LocRotScale(loc, rot, scale)


def matrix_components_from_snapshot(snapshot):
    loc, rot, scale = matrix_from_plain(snapshot["matrix_basis"]).decompose()
    return loc, rot, scale


def cached_pose_data(force_reload=False):
    path = output_path()
    if not os.path.exists(path):
        return None

    modified_time = os.path.getmtime(path)
    cache_record = _JSON_CACHE.get(path)
    if (
        cache_record
        and not force_reload
        and cache_record["modified_time"] == modified_time
    ):
        return cache_record["data"]

    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    _JSON_CACHE[path] = {
        "modified_time": modified_time,
        "data": data,
    }
    return data


def runtime_pose_cache(armature, force_reload=False):
    cache_id = cache_id_for_armature(armature)
    path = output_path()
    runtime_key = (cache_id, path)
    if runtime_key in _RUNTIME_CACHE and not force_reload:
        return _RUNTIME_CACHE[runtime_key]

    cache = cached_pose_data(force_reload=force_reload)
    if not cache:
        return None

    pose = cache.get("poses", {}).get("positive", {})
    if not pose.get("driver_bases") or not pose.get("neutral_driver_bases"):
        return {
            "cache_id": cache_id,
            "cache": cache,
            "items": [],
            "error": "Cache is old or incomplete. Re-run Test Cache eCTRLSerious.",
        }

    items = []
    for bone_name, snapshot in pose.get("driver_bases", {}).items():
        pose_bone = armature.pose.bones.get(bone_name)
        neutral = pose.get("neutral_driver_bases", {}).get(bone_name)
        if not pose_bone or not neutral:
            continue

        neutral_loc, neutral_rot, neutral_scale = matrix_components_from_snapshot(neutral)
        posed_loc, posed_rot, posed_scale = matrix_components_from_snapshot(snapshot)
        items.append(
            {
                "bone_name": bone_name,
                "pose_bone": pose_bone,
                "neutral_loc": neutral_loc,
                "neutral_rot": neutral_rot,
                "neutral_scale": neutral_scale,
                "posed_loc": posed_loc,
                "posed_rot": posed_rot,
                "posed_scale": posed_scale,
            }
        )

    runtime = {
        "cache_id": cache_id,
        "cache": cache,
        "items": items,
        "error": "",
    }
    _RUNTIME_CACHE[runtime_key] = runtime
    return runtime


def matrix_from_runtime_item(item, weight):
    loc = item["neutral_loc"].lerp(item["posed_loc"], weight)
    rot = item["neutral_rot"].slerp(item["posed_rot"], weight)
    scale = item["neutral_scale"].lerp(item["posed_scale"], weight)
    return Matrix.LocRotScale(loc, rot, scale)


def apply_cached_pose_from_file(armature, weight):
    runtime = runtime_pose_cache(armature)
    if not runtime:
        armature["daz_mhx_test_status"] = "No cache file found. Run Test Cache eCTRLSerious first."
        return
    if runtime.get("error"):
        armature["daz_mhx_test_status"] = runtime["error"]
        return

    applied_count = apply_runtime_pose_cache(runtime, weight)

    armature["daz_mhx_test_status"] = (
        f"Applied cached serious={weight:.3f} to {applied_count} driver bones "
        f"from cache {runtime['cache_id']}."
    )


def mute_constraints_from_cache(armature, cache):
    for record in cache.get("driver_records", []):
        for link in record.get("author_links", []):
            bone_name = link.get("author_bone")
            constraint_index = link.get("constraint_index")
            pose_bone = armature.pose.bones.get(bone_name)
            if not pose_bone or constraint_index is None:
                continue
            if constraint_index >= len(pose_bone.constraints):
                continue

            pose_bone.constraints[constraint_index].mute = True


def apply_bone_snapshots(armature, neutral_bones, baked_bones, weight):
    applied_count = 0
    for bone_name, snapshot in baked_bones.items():
        pose_bone = armature.pose.bones.get(bone_name)
        if not pose_bone:
            continue

        neutral = neutral_bones.get(bone_name)
        if not neutral:
            continue

        pose_bone.matrix_basis = lerp_matrix_basis(
            neutral["matrix_basis"],
            snapshot["matrix_basis"],
            weight,
        )
        applied_count += 1
    return applied_count


def apply_runtime_pose_cache(runtime, weight):
    applied_count = 0
    for item in runtime["items"]:
        item["pose_bone"].matrix_basis = matrix_from_runtime_item(item, weight)
        applied_count += 1
    return applied_count


def delete_transform_drivers_on_cached_driver_bones(armature, records):
    if not armature.animation_data:
        return 0

    driver_bones = {record["driver_bone"] for record in records}
    removed = 0
    drivers = armature.animation_data.drivers
    for fcurve in list(drivers):
        pose_path = parse_pose_bone_data_path(fcurve.data_path)
        if not pose_path:
            continue
        if pose_path["bone_name"] not in driver_bones:
            continue
        if pose_path["channel_base"] not in {
            "location",
            "rotation_euler",
            "rotation_quaternion",
            "scale",
        }:
            continue

        drivers.remove(fcurve)
        removed += 1
    return removed


def on_test_control_changed(self, context):
    armature = self
    if not armature or armature.type != "ARMATURE":
        return

    apply_cached_pose_from_file(armature, armature.serious)
    force_depsgraph_update(context)


class DAZMHX_OT_test_convert_prop(bpy.types.Operator):
    bl_idname = "daz_mhx.test_convert_prop"
    bl_label = "Test Cache eCTRLSerious"
    bl_description = "Bake one safe custom prop chain to JSON, delete matched driver-bone drivers, and apply it with the serious callback"
    bl_options = {"REGISTER", "UNDO"}

    prop_name: bpy.props.StringProperty(
        name="Custom Property",
        default=DEFAULT_PROP_NAME,
    )
    prop_value: bpy.props.FloatProperty(
        name="Bake Value",
        default=1.0,
    )
    def execute(self, context):
        armature = selected_armature(context)
        if not armature:
            self.report({"WARNING"}, "Select an armature.")
            return {"CANCELLED"}

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        cache = bake_prop_to_json(context, armature, self.prop_name, self.prop_value)

        with open(output_path(), "w", encoding="utf-8") as handle:
            json.dump(cache, handle, indent=2, sort_keys=True)

        removed = delete_transform_drivers_on_cached_driver_bones(
            armature,
            cache["driver_records"],
        )
        _JSON_CACHE.clear()
        _RUNTIME_CACHE.pop((cache_id_for_armature(armature), output_path()), None)
        runtime_pose_cache(armature, force_reload=True)
        armature["daz_mhx_test_cache_path"] = output_path()
        armature["daz_mhx_test_cache_id"] = cache_id_for_armature(armature)
        armature["daz_mhx_test_source_property"] = self.prop_name
        armature["daz_mhx_test_driver_record_count"] = cache["driver_record_count"]
        armature["daz_mhx_test_removed_driver_count"] = removed
        armature["daz_mhx_test_status"] = (
            f"Cache written and {removed} transform drivers on cached driver bones removed. "
            "Move the serious slider to apply it."
        )

        self.report(
            {"INFO"},
            (
                f"Wrote {output_path()} with {cache['author_bone_count']} author bones; "
                f"removed {removed} transform drivers on cached driver bones."
            ),
        )
        return {"FINISHED"}


class DAZMHX_PT_test_converter(bpy.types.Panel):
    bl_label = "Daz MHX Test Cache"
    bl_idname = "DAZMHX_PT_test_converter"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Daz MHX"

    def draw(self, context):
        layout = self.layout
        armature = selected_armature(context)

        layout.operator("daz_mhx.test_convert_prop")
        if armature and hasattr(armature, TEST_CONTROL_NAME):
            layout.prop(armature, TEST_CONTROL_NAME, text=TEST_CONTROL_NAME)
        if armature:
            layout.label(text=armature.get("daz_mhx_test_status", "No cache applied yet."))


classes = (
    DAZMHX_OT_test_convert_prop,
    DAZMHX_PT_test_converter,
)


def register():
    if hasattr(bpy.types.Object, "eCTRLSerious"):
        del bpy.types.Object.eCTRLSerious
    if hasattr(bpy.types.Object, "serious"):
        del bpy.types.Object.serious

    bpy.types.Object.serious = bpy.props.FloatProperty(
        name="serious",
        min=0.0,
        max=1.0,
        default=0.0,
        update=on_test_control_changed,
    )

    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    if hasattr(bpy.types.Object, "serious"):
        del bpy.types.Object.serious
    if hasattr(bpy.types.Object, "eCTRLSerious"):
        del bpy.types.Object.eCTRLSerious


if __name__ == "__main__":
    register()
