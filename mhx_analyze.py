import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime

import bpy


bl_info = {
    "name": "MHX Rig Analyzer",
    "author": "Codex",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > MHX",
    "description": "Export custom properties and drivers from selected armatures and related meshes to JSON.",
    "category": "Object",
}


OUTPUT_DIR = r"C:\Users\meat\Documents\blender\code\Daz Cleaner"
DEFAULT_OUTPUT_FILENAME = "mhx_analysis.json"
DEFAULT_BONE_DRIVER_OUTPUT_FILENAME = "mhx_bone_drivers.json"
DEFAULT_SHAPE_KEY_OUTPUT_FILENAME = "mhx_shape_keys.json"

POSE_BONE_PATH_RE = re.compile(r'pose\.bones\["([^"]+)"\]\.(.+)')
CUSTOM_PROPERTY_PATH_RE = re.compile(r'\["([^"]+)"\]')


def to_plain_value(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value

    if hasattr(value, "to_list"):
        return value.to_list()

    if hasattr(value, "to_dict"):
        return {str(key): to_plain_value(item) for key, item in value.to_dict().items()}

    if isinstance(value, (list, tuple)):
        return [to_plain_value(item) for item in value]

    try:
        return list(value)
    except TypeError:
        return str(value)


def id_block_ref(id_block):
    if not id_block:
        return None

    return {
        "name": getattr(id_block, "name", ""),
        "type": id_block.__class__.__name__,
        "library": id_block.library.filepath if getattr(id_block, "library", None) else None,
    }


def custom_property_keys(id_block):
    if not id_block:
        return []

    try:
        return list(id_block.keys())
    except TypeError:
        return []


def get_custom_property_ui_data(id_block):
    if not id_block:
        return {}

    ui_data = {}
    for key in custom_property_keys(id_block):
        try:
            data = id_block.id_properties_ui(key).as_dict()
        except Exception:
            data = {}

        if data:
            ui_data[key] = to_plain_value(data)

    return ui_data


def collect_custom_properties(id_block):
    if not id_block:
        return []

    ui_data = get_custom_property_ui_data(id_block)
    props = []
    for key in custom_property_keys(id_block):
        if key == "_RNA_UI":
            continue

        props.append(
            {
                "name": key,
                "value": to_plain_value(id_block[key]),
                "value_type": type(id_block[key]).__name__,
                "ui": ui_data.get(key, {}),
            }
        )

    return props


def fcurve_display_name(fcurve):
    if fcurve.array_index >= 0:
        return f"{fcurve.data_path}[{fcurve.array_index}]"
    return fcurve.data_path


def collect_driver_variable_target(target):
    id_block = getattr(target, "id", None)
    return {
        "id": id_block_ref(id_block),
        "id_type": getattr(target, "id_type", None),
        "data_path": getattr(target, "data_path", ""),
        "bone_target": getattr(target, "bone_target", ""),
        "transform_type": getattr(target, "transform_type", ""),
        "transform_space": getattr(target, "transform_space", ""),
        "rotation_mode": getattr(target, "rotation_mode", ""),
    }


def collect_driver(fcurve, owner_label, owner_id_block):
    driver = fcurve.driver
    return {
        "name": fcurve_display_name(fcurve),
        "owner": owner_label,
        "owner_id": id_block_ref(owner_id_block),
        "data_path": fcurve.data_path,
        "array_index": fcurve.array_index,
        "expression": driver.expression,
        "driver_type": driver.type,
        "use_self": driver.use_self,
        "mute": fcurve.mute,
        "is_valid": fcurve.is_valid,
        "variables": [
            {
                "name": variable.name,
                "type": variable.type,
                "targets": [
                    collect_driver_variable_target(target)
                    for target in variable.targets
                ],
            }
            for variable in driver.variables
        ],
    }


def parse_pose_bone_data_path(data_path):
    match = POSE_BONE_PATH_RE.match(data_path or "")
    if not match:
        return None

    bone_name, path_tail = match.groups()
    path_parts = path_tail.split(".", 1)
    channel = path_parts[0]
    channel_base = channel.split("[", 1)[0]
    sub_path = path_parts[1] if len(path_parts) > 1 else ""

    return {
        "bone_name": bone_name,
        "channel": channel,
        "channel_base": channel_base,
        "sub_path": sub_path,
        "is_driver_bone": "(drv)" in bone_name,
    }


def custom_properties_from_path(data_path):
    return CUSTOM_PROPERTY_PATH_RE.findall(data_path or "")


def collect_driver_source_refs(driver):
    refs = []
    for variable in driver.variables:
        for target in variable.targets:
            refs.append(
                {
                    "variable_name": variable.name,
                    "variable_type": variable.type,
                    "id": id_block_ref(getattr(target, "id", None)),
                    "data_path": getattr(target, "data_path", ""),
                    "custom_properties": custom_properties_from_path(
                        getattr(target, "data_path", "")
                    ),
                    "bone_target": getattr(target, "bone_target", ""),
                    "transform_type": getattr(target, "transform_type", ""),
                    "transform_space": getattr(target, "transform_space", ""),
                    "rotation_mode": getattr(target, "rotation_mode", ""),
                }
            )
    return refs


def driver_kind_from_data_path(data_path):
    pose_path = parse_pose_bone_data_path(data_path)
    if pose_path:
        if pose_path["channel_base"] in {"location", "rotation_euler", "rotation_quaternion", "scale"}:
            return "pose_bone_transform"
        if pose_path["channel_base"] == "constraints":
            return "pose_bone_constraint"
        if pose_path["channel_base"].startswith("lock_"):
            return "pose_bone_lock"
        return "pose_bone_other"

    if custom_properties_from_path(data_path):
        return "custom_property"

    return "other"


def collect_drivers(id_block, owner_label):
    if not id_block or not id_block.animation_data:
        return []

    return [
        collect_driver(fcurve, owner_label, id_block)
        for fcurve in id_block.animation_data.drivers
    ]


def collect_copy_transform_links(armature):
    links = []
    for pose_bone in armature.pose.bones:
        for index, constraint in enumerate(pose_bone.constraints):
            if constraint.type != "COPY_TRANSFORMS":
                continue

            links.append(
                {
                    "author_bone": pose_bone.name,
                    "constraint_index": index,
                    "constraint_name": constraint.name,
                    "target": id_block_ref(getattr(constraint, "target", None)),
                    "target_bone": getattr(constraint, "subtarget", ""),
                    "target_is_selected_armature": getattr(constraint, "target", None) == armature,
                    "target_is_driver_bone": "(drv)" in getattr(constraint, "subtarget", ""),
                    "influence": constraint.influence,
                    "mute": constraint.mute,
                    "owner_space": getattr(constraint, "owner_space", ""),
                    "target_space": getattr(constraint, "target_space", ""),
                    "mix_mode": getattr(constraint, "mix_mode", ""),
                }
            )
    return links


def prop_name_category(name):
    if name.endswith("(fin)"):
        return "final_computed"
    if "(rst)" in name:
        return "rest_intermediate"
    if re.search(r":\d+$", name):
        return "numbered_component"
    if name.startswith(("eCTRL", "ECTRL")):
        return "expression_control"
    if name.startswith(("CTRL", "pCTRL")):
        return "body_or_pose_control"
    if name.startswith("facs_"):
        return "facs"
    if name.startswith(("PBM", "PHM", "FBM")):
        return "daz_morph"
    if name.startswith("Mha"):
        return "mhx_rig_setting"
    if name.startswith(("G 101", "SG", "HG", "GR", "SCR")):
        return "vendor_or_custom_asset"
    return "other"


def shape_key_name_category(name):
    lower = name.lower()
    if name == "Basis":
        return "basis"
    if name.startswith(("pJCM", "JCM", "MCM")) or "jcm" in lower:
        return "joint_corrective"
    if "corrective" in lower or lower.startswith(("adj", "fix")):
        return "corrective"
    if name.startswith(("eCTRL", "ECTRL")):
        return "expression_control"
    if name.startswith(("facs_", "FACS")):
        return "facs"
    if name.startswith(("PBM", "PHM", "FBM")):
        return "daz_morph"
    if name.startswith("Mha"):
        return "mhx_rig_setting"
    if name.startswith(("CTRL", "pCTRL")):
        return "pose_control"
    if re.match(r"^[lr][A-Z]", name):
        return "side_named"
    return "other"


def compact_driver_record(record):
    source_properties = []
    source_bones = []
    for source_ref in record["source_refs"]:
        source_properties.extend(source_ref["custom_properties"])
        if source_ref["bone_target"]:
            source_bones.append(source_ref["bone_target"])

    return {
        "name": record["name"],
        "owner": record["owner"],
        "kind": record["kind"],
        "data_path": record["data_path"],
        "array_index": record["array_index"],
        "expression": record["expression"],
        "driver_type": record["driver_type"],
        "pose_bone_target": record["pose_bone_target"],
        "driven_custom_properties": record["driven_custom_properties"],
        "source_properties": sorted(set(source_properties)),
        "source_bones": sorted(set(source_bones)),
    }


def collect_custom_property_driver_graph(custom_property_records, bake_candidates):
    driven_counts = Counter()
    source_counts = Counter()
    expression_counts = Counter()
    edges = []
    incoming = defaultdict(Counter)

    for record in custom_property_records:
        expression_counts[record["expression"]] += 1
        driven_props = record["driven_custom_properties"]
        source_props = []
        for source_ref in record["source_refs"]:
            source_props.extend(source_ref["custom_properties"])

        for driven_prop in driven_props:
            driven_counts[driven_prop] += 1
            for source_prop in source_props:
                source_counts[source_prop] += 1
                incoming[driven_prop][source_prop] += 1
                edges.append((source_prop, driven_prop, record["expression"]))

    bake_source_props = {candidate["source_property"] for candidate in bake_candidates}
    bridge_props = sorted(set(driven_counts) & bake_source_props)

    def bridge_score(prop_name):
        return source_counts[prop_name] + driven_counts[prop_name]

    return {
        "summary": {
            "driver_count": len(custom_property_records),
            "unique_driven_property_count": len(driven_counts),
            "unique_source_property_count": len(source_counts),
            "edge_count": len(edges),
            "driven_final_property_count": sum(
                count for name, count in driven_counts.items() if name.endswith("(fin)")
            ),
            "driven_rest_property_count": sum(
                count for name, count in driven_counts.items() if "(rst)" in name
            ),
            "bridge_property_count": len(bridge_props),
        },
        "driven_category_counts": dict(
            Counter(
                prop_name_category(name)
                for name, count in driven_counts.items()
                for _ in range(count)
            )
        ),
        "source_category_counts": dict(
            Counter(
                prop_name_category(name)
                for name, count in source_counts.items()
                for _ in range(count)
            )
        ),
        "top_source_properties": [
            {"name": name, "category": prop_name_category(name), "ref_count": count}
            for name, count in source_counts.most_common(100)
        ],
        "top_expressions": [
            {"expression": expression, "count": count}
            for expression, count in expression_counts.most_common(100)
        ],
        "bridge_properties": [
            {
                "name": name,
                "category": prop_name_category(name),
                "driven_driver_count": driven_counts[name],
                "source_ref_count": source_counts[name],
                "incoming_sources": [
                    {"name": source, "ref_count": count}
                    for source, count in incoming[name].most_common(12)
                ],
            }
            for name in sorted(bridge_props, key=bridge_score, reverse=True)[:250]
        ],
    }


def compact_bake_candidate(candidate):
    author_bones = sorted(
        {
            link["author_bone"]
            for links in candidate["author_bone_links"].values()
            for link in links
        }
    )
    driven_channel_samples = candidate["driven_channels"][:12]

    return {
        "source_property": candidate["source_property"],
        "source_category": prop_name_category(candidate["source_property"]),
        "driven_channel_count": candidate["driven_channel_count"],
        "driver_bone_count": len(candidate["driver_bones"]),
        "author_bone_count": len(author_bones),
        "fully_maps_to_author_bones": candidate["fully_maps_to_author_bones"],
        "driver_bones": candidate["driver_bones"],
        "author_bones": author_bones,
        "driven_channel_samples": driven_channel_samples,
    }


def collect_armature_driver_simplification_data(armature):
    driver_records = []
    source_prop_to_bone_channels = defaultdict(list)
    driven_bone_to_drivers = defaultdict(list)
    driven_custom_props = []
    driven_constraints = []
    driven_locks = []

    for owner_label, id_block in (
        (f"armature.object:{armature.name}", armature),
        (f"armature.data:{armature.name}", armature.data),
    ):
        if not id_block or not id_block.animation_data:
            continue

        for fcurve in id_block.animation_data.drivers:
            driver = fcurve.driver
            data_path = fcurve.data_path
            kind = driver_kind_from_data_path(data_path)
            pose_path = parse_pose_bone_data_path(data_path)
            source_refs = collect_driver_source_refs(driver)

            record = {
                "name": fcurve_display_name(fcurve),
                "owner": owner_label,
                "owner_id": id_block_ref(id_block),
                "kind": kind,
                "data_path": data_path,
                "array_index": fcurve.array_index,
                "expression": driver.expression,
                "driver_type": driver.type,
                "mute": fcurve.mute,
                "is_valid": fcurve.is_valid,
                "pose_bone_target": pose_path,
                "driven_custom_properties": custom_properties_from_path(data_path),
                "source_refs": source_refs,
            }
            driver_records.append(record)

            if kind == "pose_bone_transform" and pose_path:
                driven_key = (
                    f'{pose_path["bone_name"]}.'
                    f'{pose_path["channel"]}[{fcurve.array_index}]'
                )
                driven_bone_to_drivers[pose_path["bone_name"]].append(record["name"])
                for source_ref in source_refs:
                    for prop_name in source_ref["custom_properties"]:
                        source_prop_to_bone_channels[prop_name].append(
                            {
                                "driven_driver_bone": pose_path["bone_name"],
                                "driven_channel": pose_path["channel"],
                                "array_index": fcurve.array_index,
                                "driven_key": driven_key,
                                "expression": driver.expression,
                                "variable_name": source_ref["variable_name"],
                                "variable_type": source_ref["variable_type"],
                            }
                        )
            elif kind == "custom_property":
                driven_custom_props.append(record)
            elif kind == "pose_bone_constraint":
                driven_constraints.append(record)
            elif kind == "pose_bone_lock":
                driven_locks.append(record)

    copy_links = collect_copy_transform_links(armature)
    driver_bone_to_author_bones = defaultdict(list)
    for link in copy_links:
        if link["target_bone"]:
            driver_bone_to_author_bones[link["target_bone"]].append(link)

    bake_candidates = []
    for source_prop, driven_channels in sorted(source_prop_to_bone_channels.items()):
        author_bones = {}
        driver_bones = sorted({item["driven_driver_bone"] for item in driven_channels})
        for driver_bone in driver_bones:
            links = driver_bone_to_author_bones.get(driver_bone, [])
            if links:
                author_bones[driver_bone] = [
                    {
                        "author_bone": link["author_bone"],
                        "constraint_name": link["constraint_name"],
                        "owner_space": link["owner_space"],
                        "target_space": link["target_space"],
                        "influence": link["influence"],
                        "mute": link["mute"],
                    }
                    for link in links
                ]

        bake_candidates.append(
            {
                "source_property": source_prop,
                "driven_channel_count": len(driven_channels),
                "driver_bones": driver_bones,
                "author_bone_links": author_bones,
                "fully_maps_to_author_bones": bool(author_bones)
                and all(driver_bone in author_bones for driver_bone in driver_bones),
                "driven_channels": driven_channels,
            }
        )

    removable_driver_bone_candidates = []
    for driver_bone, driver_names in sorted(driven_bone_to_drivers.items()):
        links = driver_bone_to_author_bones.get(driver_bone, [])
        if not links:
            continue

        removable_driver_bone_candidates.append(
            {
                "driver_bone": driver_bone,
                "driver_count": len(driver_names),
                "driver_names": driver_names,
                "author_bones": [
                    {
                        "author_bone": link["author_bone"],
                        "constraint_name": link["constraint_name"],
                        "owner_space": link["owner_space"],
                        "target_space": link["target_space"],
                        "influence": link["influence"],
                        "mute": link["mute"],
                    }
                    for link in links
                ],
                "safe_shape": (
                    all(link["target_is_selected_armature"] for link in links)
                    and all(link["target_is_driver_bone"] for link in links)
                    and all(link["owner_space"] == "LOCAL" for link in links)
                    and all(link["target_space"] == "LOCAL" for link in links)
                    and all(abs(link["influence"] - 1.0) < 0.0001 for link in links)
                    and not any(link["mute"] for link in links)
                ),
            }
        )

    kind_counts = Counter(record["kind"] for record in driver_records)
    expression_counts = Counter(record["expression"] for record in driver_records)
    source_prop_counts = Counter()
    source_bone_counts = Counter()
    driven_channel_counts = Counter()
    for record in driver_records:
        pose_target = record.get("pose_bone_target")
        if pose_target:
            driven_channel_counts[pose_target["channel"]] += 1
        for source_ref in record["source_refs"]:
            for prop_name in source_ref["custom_properties"]:
                source_prop_counts[prop_name] += 1
            if source_ref["bone_target"]:
                source_bone_counts[source_ref["bone_target"]] += 1

    fully_mapped_bake_candidates = [
        candidate for candidate in bake_candidates if candidate["fully_maps_to_author_bones"]
    ]
    partial_bake_candidates = [
        candidate for candidate in bake_candidates if not candidate["fully_maps_to_author_bones"]
    ]
    safe_removable_driver_bones = [
        candidate for candidate in removable_driver_bone_candidates if candidate["safe_shape"]
    ]

    return {
        "summary": {
            "armature_driver_count": len(driver_records),
            "driver_kind_counts": dict(kind_counts),
            "driven_channel_counts": dict(driven_channel_counts),
            "copy_transform_link_count": len(copy_links),
            "copy_transform_links_to_driver_bones": sum(
                1 for link in copy_links if link["target_is_driver_bone"]
            ),
            "bake_candidate_source_property_count": len(bake_candidates),
            "fully_mapped_bake_candidate_count": len(fully_mapped_bake_candidates),
            "partial_bake_candidate_count": len(partial_bake_candidates),
            "removable_driver_bone_candidate_count": len(removable_driver_bone_candidates),
            "safe_removable_driver_bone_candidate_count": len(safe_removable_driver_bones),
        },
        "top_source_properties": [
            {"name": name, "driver_ref_count": count}
            for name, count in source_prop_counts.most_common(100)
        ],
        "top_source_bones": [
            {"name": name, "driver_ref_count": count}
            for name, count in source_bone_counts.most_common(100)
        ],
        "top_expressions": [
            {"expression": expression, "count": count}
            for expression, count in expression_counts.most_common(100)
        ],
        "custom_property_driver_graph": collect_custom_property_driver_graph(
            driven_custom_props,
            bake_candidates,
        ),
        "copy_transform_links_to_driver_bones": [
            link for link in copy_links if link["target_is_driver_bone"]
        ],
        "fully_mapped_bake_candidates": [
            compact_bake_candidate(candidate)
            for candidate in sorted(
                fully_mapped_bake_candidates,
                key=lambda item: item["driven_channel_count"],
                reverse=True,
            )
        ],
        "partial_bake_candidates": [
            compact_bake_candidate(candidate)
            for candidate in sorted(
                partial_bake_candidates,
                key=lambda item: item["driven_channel_count"],
                reverse=True,
            )
        ],
        "removable_driver_bone_candidates": [
            {
                "driver_bone": candidate["driver_bone"],
                "driver_count": candidate["driver_count"],
                "author_bones": [
                    link["author_bone"] for link in candidate["author_bones"]
                ],
                "safe_shape": candidate["safe_shape"],
            }
            for candidate in removable_driver_bone_candidates
        ],
        "constraint_driver_records": [
            compact_driver_record(record) for record in driven_constraints
        ],
        "lock_driver_records": [
            compact_driver_record(record) for record in driven_locks
        ],
    }


def compact_source_refs(source_refs):
    refs = []
    for source_ref in source_refs:
        props = source_ref["custom_properties"]
        if props:
            refs.extend(
                {
                    "variable": source_ref["variable_name"],
                    "type": source_ref["variable_type"],
                    "property": prop_name,
                    "data_path": source_ref["data_path"],
                }
                for prop_name in props
            )
            continue

        if source_ref["bone_target"] or source_ref["data_path"]:
            refs.append(
                {
                    "variable": source_ref["variable_name"],
                    "type": source_ref["variable_type"],
                    "bone": source_ref["bone_target"],
                    "data_path": source_ref["data_path"],
                    "transform_type": source_ref["transform_type"],
                    "transform_space": source_ref["transform_space"],
                }
            )

    return refs


def collect_compact_bone_driver_report(armature):
    bones = defaultdict(list)
    channel_counts = Counter()
    expression_counts = Counter()
    owner_counts = Counter()
    driver_count = 0

    for owner_label, id_block in (
        ("object", armature),
        ("data", armature.data),
    ):
        if not id_block or not id_block.animation_data:
            continue

        for fcurve in id_block.animation_data.drivers:
            pose_path = parse_pose_bone_data_path(fcurve.data_path)
            if not pose_path:
                continue

            driver = fcurve.driver
            driver_count += 1
            channel_counts[pose_path["channel_base"]] += 1
            expression_counts[driver.expression] += 1
            owner_counts[owner_label] += 1

            bones[pose_path["bone_name"]].append(
                {
                    "owner": owner_label,
                    "channel": pose_path["channel_base"],
                    "array_index": fcurve.array_index,
                    "expression": driver.expression,
                    "variables": compact_source_refs(collect_driver_source_refs(driver)),
                }
            )

    return {
        "armature": armature.name,
        "summary": {
            "bone_driver_count": driver_count,
            "driven_bone_count": len(bones),
            "owner_counts": dict(owner_counts),
            "channel_counts": dict(channel_counts),
            "top_expressions": [
                {"expression": expression, "count": count}
                for expression, count in expression_counts.most_common(25)
            ],
        },
        "bones": [
            {
                "name": bone_name,
                "driver_count": len(drivers),
                "drivers": sorted(
                    drivers,
                    key=lambda item: (
                        item["owner"],
                        item["channel"],
                        item["array_index"],
                        item["expression"],
                    ),
                ),
            }
            for bone_name, drivers in sorted(bones.items(), key=lambda item: item[0].lower())
        ],
    }


def shape_key_name_from_data_path(data_path):
    match = re.search(r'key_blocks\["([^"]+)"\]\.value', data_path or "")
    return match.group(1) if match else None


def compact_shape_key_driver(fcurve):
    driver = fcurve.driver
    return {
        "expression": driver.expression,
        "array_index": fcurve.array_index,
        "variables": compact_source_refs(collect_driver_source_refs(driver)),
    }


def collect_compact_shape_key_report(armature):
    meshes = []
    total_shape_keys = 0
    driven_shape_keys = 0
    total_drivers = 0
    category_counts = Counter()
    driven_category_counts = Counter()
    source_prop_counts = Counter()
    source_category_counts = Counter()
    expression_counts = Counter()

    for mesh_obj in related_meshes_for_armature(armature):
        shape_keys = getattr(mesh_obj.data, "shape_keys", None)
        if not shape_keys:
            continue

        drivers_by_shape = defaultdict(list)
        if shape_keys.animation_data:
            for fcurve in shape_keys.animation_data.drivers:
                shape_name = shape_key_name_from_data_path(fcurve.data_path)
                if not shape_name:
                    continue

                driver_record = compact_shape_key_driver(fcurve)
                drivers_by_shape[shape_name].append(driver_record)
                total_drivers += 1
                expression_counts[driver_record["expression"]] += 1
                for variable in driver_record["variables"]:
                    prop_name = variable.get("property")
                    if prop_name:
                        source_prop_counts[prop_name] += 1
                        source_category_counts[prop_name_category(prop_name)] += 1

        sparse_keys = []
        for key_block in shape_keys.key_blocks:
            name = key_block.name
            if name == "Basis":
                continue

            total_shape_keys += 1
            category = shape_key_name_category(name)
            category_counts[category] += 1
            drivers = drivers_by_shape.get(name, [])
            if drivers:
                driven_shape_keys += 1
                driven_category_counts[category] += 1

            sparse_keys.append(
                {
                    "name": name,
                    "category": category,
                    "driver_count": len(drivers),
                    "drivers": drivers,
                }
            )

        meshes.append(
            {
                "name": mesh_obj.name,
                "shape_key_count": len(sparse_keys),
                "driven_shape_key_count": sum(
                    1 for item in sparse_keys if item["driver_count"]
                ),
                "shape_keys": sorted(
                    sparse_keys,
                    key=lambda item: (
                        item["category"],
                        item["name"].lower(),
                    ),
                ),
            }
        )

    return {
        "armature": armature.name,
        "summary": {
            "mesh_count": len(meshes),
            "shape_key_count": total_shape_keys,
            "driven_shape_key_count": driven_shape_keys,
            "shape_key_driver_count": total_drivers,
            "category_counts": dict(category_counts),
            "driven_category_counts": dict(driven_category_counts),
            "source_category_counts": dict(source_category_counts),
            "top_source_properties": [
                {"name": name, "ref_count": count, "category": prop_name_category(name)}
                for name, count in source_prop_counts.most_common(50)
            ],
            "top_expressions": [
                {"expression": expression, "count": count}
                for expression, count in expression_counts.most_common(25)
            ],
        },
        "meshes": meshes,
    }


def collect_bone_data(armature):
    bones = []
    data_bones = armature.data.bones if armature.data else {}

    for pose_bone in armature.pose.bones:
        data_bone = data_bones.get(pose_bone.name) if hasattr(data_bones, "get") else None
        bones.append(
            {
                "name": pose_bone.name,
                "parent": pose_bone.parent.name if pose_bone.parent else None,
                "bone_collection_names": [
                    collection.name for collection in getattr(pose_bone.bone, "collections", [])
                ],
                "custom_properties": {
                    "pose_bone": collect_custom_properties(pose_bone),
                    "data_bone": collect_custom_properties(data_bone),
                },
                "constraints": [
                    {
                        "name": constraint.name,
                        "type": constraint.type,
                        "influence": constraint.influence,
                        "mute": constraint.mute,
                        "target": id_block_ref(getattr(constraint, "target", None)),
                        "subtarget": getattr(constraint, "subtarget", ""),
                        "owner_space": getattr(constraint, "owner_space", ""),
                        "target_space": getattr(constraint, "target_space", ""),
                    }
                    for constraint in pose_bone.constraints
                ],
            }
        )

    return bones


def mesh_uses_armature(mesh_obj, armature):
    if mesh_obj.parent == armature:
        return True

    for modifier in mesh_obj.modifiers:
        if modifier.type == "ARMATURE" and modifier.object == armature:
            return True

    return False


def related_meshes_for_armature(armature):
    meshes = []
    for obj in bpy.context.scene.objects:
        if obj.type == "MESH" and mesh_uses_armature(obj, armature):
            meshes.append(obj)
    return meshes


def collect_shape_key_data(mesh_obj):
    shape_keys = getattr(mesh_obj.data, "shape_keys", None)
    if not shape_keys:
        return None

    return {
        "id": id_block_ref(shape_keys),
        "custom_properties": collect_custom_properties(shape_keys),
        "drivers": collect_drivers(shape_keys, f"mesh.shape_keys:{mesh_obj.name}"),
        "key_blocks": [
            {
                "name": key_block.name,
                "value": key_block.value,
                "slider_min": key_block.slider_min,
                "slider_max": key_block.slider_max,
                "mute": key_block.mute,
                "relative_key": key_block.relative_key.name if key_block.relative_key else None,
                "custom_properties": collect_custom_properties(key_block),
            }
            for key_block in shape_keys.key_blocks
        ],
    }


def collect_mesh_data(mesh_obj):
    return {
        "name": mesh_obj.name,
        "object": {
            "id": id_block_ref(mesh_obj),
            "custom_properties": collect_custom_properties(mesh_obj),
            "drivers": collect_drivers(mesh_obj, f"mesh.object:{mesh_obj.name}"),
        },
        "data": {
            "id": id_block_ref(mesh_obj.data),
            "custom_properties": collect_custom_properties(mesh_obj.data),
            "drivers": collect_drivers(mesh_obj.data, f"mesh.data:{mesh_obj.name}"),
        },
        "shape_keys": collect_shape_key_data(mesh_obj),
    }


def collect_armature_data(armature):
    return {
        "name": armature.name,
        "object": {
            "id": id_block_ref(armature),
            "custom_properties": collect_custom_properties(armature),
            "drivers": collect_drivers(armature, f"armature.object:{armature.name}"),
        },
        "data": {
            "id": id_block_ref(armature.data),
            "custom_properties": collect_custom_properties(armature.data),
            "drivers": collect_drivers(armature.data, f"armature.data:{armature.name}"),
        },
        "pose_bones": collect_bone_data(armature),
        "simplification_analysis": collect_armature_driver_simplification_data(armature),
    }


def collect_related_mesh_summary(mesh_obj):
    shape_keys = getattr(mesh_obj.data, "shape_keys", None)
    return {
        "name": mesh_obj.name,
        "object_custom_property_count": len(collect_custom_properties(mesh_obj)),
        "data_custom_property_count": len(collect_custom_properties(mesh_obj.data)),
        "object_driver_count": len(collect_drivers(mesh_obj, f"mesh.object:{mesh_obj.name}")),
        "data_driver_count": len(collect_drivers(mesh_obj.data, f"mesh.data:{mesh_obj.name}")),
        "shape_key_count": len(shape_keys.key_blocks) if shape_keys else 0,
        "shape_key_driver_count": len(
            collect_drivers(shape_keys, f"mesh.shape_keys:{mesh_obj.name}")
        )
        if shape_keys
        else 0,
    }


def collect_armature_compact_data(armature):
    pose_bone_count = len(armature.pose.bones) if armature.pose else 0
    return {
        "name": armature.name,
        "object": {
            "id": id_block_ref(armature),
            "custom_property_count": len(collect_custom_properties(armature)),
            "driver_count": len(collect_drivers(armature, f"armature.object:{armature.name}")),
        },
        "data": {
            "id": id_block_ref(armature.data),
            "custom_property_count": len(collect_custom_properties(armature.data)),
            "driver_count": len(collect_drivers(armature.data, f"armature.data:{armature.name}")),
        },
        "pose_bone_count": pose_bone_count,
        "simplification_analysis": collect_armature_driver_simplification_data(armature),
    }


def selected_armatures(context):
    return [obj for obj in context.selected_objects if obj.type == "ARMATURE"]


def build_report(context):
    armatures = selected_armatures(context)
    return {
        "schema_version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "blend_file": bpy.data.filepath,
        "selected_armatures": [
            {
                "armature": collect_armature_compact_data(armature),
                "related_meshes_summary": [
                    collect_related_mesh_summary(mesh_obj)
                    for mesh_obj in related_meshes_for_armature(armature)
                ],
            }
            for armature in armatures
        ],
    }


def build_bone_driver_report(context):
    armatures = selected_armatures(context)
    return {
        "schema_version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "blend_file": bpy.data.filepath,
        "selected_armatures": [
            collect_compact_bone_driver_report(armature)
            for armature in armatures
        ],
    }


def build_shape_key_report(context):
    armatures = selected_armatures(context)
    return {
        "schema_version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "blend_file": bpy.data.filepath,
        "selected_armatures": [
            collect_compact_shape_key_report(armature)
            for armature in armatures
        ],
    }


def default_output_path():
    return os.path.join(OUTPUT_DIR, DEFAULT_OUTPUT_FILENAME)


def default_bone_driver_output_path():
    return os.path.join(OUTPUT_DIR, DEFAULT_BONE_DRIVER_OUTPUT_FILENAME)


def default_shape_key_output_path():
    return os.path.join(OUTPUT_DIR, DEFAULT_SHAPE_KEY_OUTPUT_FILENAME)


class MHX_OT_export_analysis_json(bpy.types.Operator):
    bl_idname = "mhx.export_analysis_json"
    bl_label = "Export MHX Analysis JSON"
    bl_description = "Export custom properties and drivers from selected armatures and related meshes"
    bl_options = {"REGISTER"}

    output_path: bpy.props.StringProperty(
        name="Output Path",
        subtype="FILE_PATH",
        default=default_output_path(),
    )

    def execute(self, context):
        if not selected_armatures(context):
            self.report({"WARNING"}, "Select at least one armature.")
            return {"CANCELLED"}

        path = bpy.path.abspath(self.output_path)
        output_dir = os.path.dirname(path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        with open(path, "w", encoding="utf-8") as handle:
            json.dump(build_report(context), handle, indent=2, sort_keys=True)

        self.report({"INFO"}, f"Wrote MHX analysis JSON: {path}")
        return {"FINISHED"}


class MHX_OT_export_bone_drivers_json(bpy.types.Operator):
    bl_idname = "mhx.export_bone_drivers_json"
    bl_label = "Export Bone Drivers JSON"
    bl_description = "Export a compact list of pose-bone drivers grouped by driven bone"
    bl_options = {"REGISTER"}

    output_path: bpy.props.StringProperty(
        name="Output Path",
        subtype="FILE_PATH",
        default=default_bone_driver_output_path(),
    )

    def execute(self, context):
        if not selected_armatures(context):
            self.report({"WARNING"}, "Select at least one armature.")
            return {"CANCELLED"}

        path = bpy.path.abspath(self.output_path)
        output_dir = os.path.dirname(path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        with open(path, "w", encoding="utf-8") as handle:
            json.dump(build_bone_driver_report(context), handle, indent=2, sort_keys=True)

        self.report({"INFO"}, f"Wrote compact bone driver JSON: {path}")
        return {"FINISHED"}


class MHX_OT_export_shape_keys_json(bpy.types.Operator):
    bl_idname = "mhx.export_shape_keys_json"
    bl_label = "Export Shape Keys JSON"
    bl_description = "Export a compact list of shape keys, drivers, expressions, and variable sources"
    bl_options = {"REGISTER"}

    output_path: bpy.props.StringProperty(
        name="Output Path",
        subtype="FILE_PATH",
        default=default_shape_key_output_path(),
    )

    def execute(self, context):
        if not selected_armatures(context):
            self.report({"WARNING"}, "Select at least one armature.")
            return {"CANCELLED"}

        path = bpy.path.abspath(self.output_path)
        output_dir = os.path.dirname(path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        with open(path, "w", encoding="utf-8") as handle:
            json.dump(build_shape_key_report(context), handle, indent=2, sort_keys=True)

        self.report({"INFO"}, f"Wrote compact shape key JSON: {path}")
        return {"FINISHED"}


class MHX_PT_rig_analyzer(bpy.types.Panel):
    bl_label = "MHX Rig Analyzer"
    bl_idname = "MHX_PT_rig_analyzer"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MHX"

    def draw(self, context):
        layout = self.layout
        selected_count = len(selected_armatures(context))

        layout.label(text=f"Selected armatures: {selected_count}")
        layout.operator("mhx.export_analysis_json", text="Export Analysis JSON")
        layout.operator("mhx.export_bone_drivers_json", text="Export Bone Drivers JSON")
        layout.operator("mhx.export_shape_keys_json", text="Export Shape Keys JSON")


classes = (
    MHX_OT_export_analysis_json,
    MHX_OT_export_bone_drivers_json,
    MHX_OT_export_shape_keys_json,
    MHX_PT_rig_analyzer,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
