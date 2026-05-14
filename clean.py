import bpy


bl_info = {
    "name": "Selection Cleaner",
    "author": "Codex",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Clean",
    "description": "Delete object and bone cleanup data from selected objects.",
    "category": "Object",
}


def clear_animation_drivers(id_block):
    if not id_block or not id_block.animation_data:
        return 0

    removed = 0
    drivers = id_block.animation_data.drivers
    for fcurve in list(drivers):
        drivers.remove(fcurve)
        removed += 1
    return removed


def set_driver_mute(id_block, mute, predicate=None):
    if not id_block or not id_block.animation_data:
        return 0

    changed = 0
    for fcurve in id_block.animation_data.drivers:
        if predicate and not predicate(fcurve):
            continue
        if fcurve.mute == mute:
            continue
        fcurve.mute = mute
        changed += 1
    return changed


def is_bone_driver_fcurve(fcurve):
    return fcurve.data_path.startswith("pose.bones[")


def set_armature_driver_mute(armature, mute):
    changed = set_driver_mute(
        armature,
        mute,
        predicate=lambda fcurve: not is_bone_driver_fcurve(fcurve),
    )
    changed += set_driver_mute(armature.data, mute)
    return changed


def set_bone_driver_mute(armature, mute):
    return set_driver_mute(
        armature,
        mute,
        predicate=is_bone_driver_fcurve,
    )


def clear_modifiers(obj):
    removed = len(obj.modifiers)
    for modifier in list(obj.modifiers):
        obj.modifiers.remove(modifier)
    return removed


def clear_constraints(obj):
    removed = len(obj.constraints)
    for constraint in list(obj.constraints):
        obj.constraints.remove(constraint)
    return removed


def clear_drivers(obj):
    removed = clear_animation_drivers(obj)
    removed += clear_animation_drivers(obj.data) if getattr(obj, "data", None) else 0
    return removed


def clear_custom_properties(id_block):
    if not id_block:
        return 0

    removed = 0
    for key in list(id_block.keys()):
        del id_block[key]
        removed += 1
    return removed


def clear_object_custom_properties(obj):
    removed = clear_custom_properties(obj)
    removed += clear_custom_properties(obj.data) if getattr(obj, "data", None) else 0
    return removed


def armatures_with_bones(objects):
    armatures = []
    for obj in objects:
        if obj.type == "ARMATURE" and obj.pose and obj.pose.bones:
            armatures.append(obj)
    return armatures


def has_selected_armature_bones(context):
    return bool(armatures_with_bones(context.selected_objects))


def clear_bone_constraints(armature):
    removed = 0
    for pose_bone in armature.pose.bones:
        removed += len(pose_bone.constraints)
        for constraint in list(pose_bone.constraints):
            pose_bone.constraints.remove(constraint)
    return removed


def clear_bone_custom_properties(armature):
    removed = 0
    data_bones = armature.data.bones if armature.data else {}

    for pose_bone in armature.pose.bones:
        removed += clear_custom_properties(pose_bone)

        data_bone = data_bones.get(pose_bone.name) if hasattr(data_bones, "get") else None
        removed += clear_custom_properties(data_bone)

    return removed


def clear_bone_drivers(armature):
    if not armature.animation_data:
        return 0

    removed = 0
    drivers = armature.animation_data.drivers
    for fcurve in list(drivers):
        if fcurve.data_path.startswith("pose.bones["):
            drivers.remove(fcurve)
            removed += 1
    return removed


def clear_bone_stack(
    armature,
    delete_constraints=True,
    delete_drivers=True,
    delete_custom_properties=True,
):
    removed_constraints = clear_bone_constraints(armature) if delete_constraints else 0
    removed_drivers = clear_bone_drivers(armature) if delete_drivers else 0
    removed_custom_properties = clear_bone_custom_properties(armature) if delete_custom_properties else 0

    return removed_constraints, removed_drivers, removed_custom_properties


def clear_shape_keys(obj):
    data = getattr(obj, "data", None)
    if not data or not getattr(data, "shape_keys", None):
        return 0

    removed = len(data.shape_keys.key_blocks)
    for shape_key in reversed(data.shape_keys.key_blocks):
        obj.shape_key_remove(shape_key)
    return removed


def clear_object_stack(
    obj,
    delete_modifiers=True,
    delete_constraints=True,
    delete_drivers=True,
    delete_custom_properties=True,
    delete_shape_keys=True,
):
    removed_modifiers = clear_modifiers(obj) if delete_modifiers else 0
    removed_constraints = clear_constraints(obj) if delete_constraints else 0
    removed_drivers = clear_drivers(obj) if delete_drivers else 0
    removed_custom_properties = clear_object_custom_properties(obj) if delete_custom_properties else 0
    removed_shape_keys = clear_shape_keys(obj) if delete_shape_keys else 0

    return (
        removed_modifiers,
        removed_constraints,
        removed_drivers,
        removed_custom_properties,
        removed_shape_keys,
    )


class CLEAN_OT_delete_selected(bpy.types.Operator):
    bl_idname = "clean.delete_selected"
    bl_label = "Delete From Selection"
    bl_description = "Delete the chosen item type from selected objects"
    bl_options = {"REGISTER", "UNDO"}

    cleanup_type: bpy.props.EnumProperty(
        name="Cleanup Type",
        items=[
            ("MODIFIERS", "Modifiers", "Delete modifiers from selected objects"),
            ("CONSTRAINTS", "Constraints", "Delete constraints from selected objects"),
            ("DRIVERS", "Drivers", "Delete drivers from selected objects and their data"),
            ("CUSTOM_PROPERTIES", "Custom Properties", "Delete custom properties from selected objects and their data"),
            ("SHAPE_KEYS", "Shape Keys", "Delete shape keys from selected objects"),
            ("BONE_CONSTRAINTS", "Bone Constraints", "Delete constraints from bones on selected armatures"),
            ("BONE_DRIVERS", "Bone Drivers", "Delete pose bone drivers from selected armatures"),
            ("BONE_CUSTOM_PROPERTIES", "Bone Custom Properties", "Delete custom properties from bones on selected armatures"),
            ("BONE_ALL", "All Bone Data", "Delete constraints, drivers, and custom properties from bones on selected armatures"),
            ("ALL", "All", "Delete modifiers, constraints, drivers, custom properties, and shape keys"),
        ],
        default="ALL",
    )

    def execute(self, context):
        selected = list(context.selected_objects)
        if not selected:
            self.report({"WARNING"}, "No objects selected.")
            return {"CANCELLED"}

        if self.cleanup_type.startswith("BONE_"):
            armatures = armatures_with_bones(selected)
            if not armatures:
                self.report({"WARNING"}, "No selected armatures with bones.")
                return {"CANCELLED"}

            delete_constraints = self.cleanup_type in {"BONE_CONSTRAINTS", "BONE_ALL"}
            delete_drivers = self.cleanup_type in {"BONE_DRIVERS", "BONE_ALL"}
            delete_custom_properties = self.cleanup_type in {"BONE_CUSTOM_PROPERTIES", "BONE_ALL"}

            totals = [0, 0, 0]
            for armature in armatures:
                removed = clear_bone_stack(
                    armature,
                    delete_constraints=delete_constraints,
                    delete_drivers=delete_drivers,
                    delete_custom_properties=delete_custom_properties,
                )
                totals = [a + b for a, b in zip(totals, removed)]

            self.report(
                {"INFO"},
                (
                    f"Removed {totals[0]} bone constraints, {totals[1]} bone drivers, "
                    f"{totals[2]} bone custom properties from {len(armatures)} armature(s)."
                ),
            )
            return {"FINISHED"}

        delete_modifiers = self.cleanup_type in {"MODIFIERS", "ALL"}
        delete_constraints = self.cleanup_type in {"CONSTRAINTS", "ALL"}
        delete_drivers = self.cleanup_type in {"DRIVERS", "ALL"}
        delete_custom_properties = self.cleanup_type in {"CUSTOM_PROPERTIES", "ALL"}
        delete_shape_keys = self.cleanup_type in {"SHAPE_KEYS", "ALL"}

        totals = [0, 0, 0, 0, 0]
        for obj in selected:
            removed = clear_object_stack(
                obj,
                delete_modifiers=delete_modifiers,
                delete_constraints=delete_constraints,
                delete_drivers=delete_drivers,
                delete_custom_properties=delete_custom_properties,
                delete_shape_keys=delete_shape_keys,
            )
            totals = [a + b for a, b in zip(totals, removed)]

        self.report(
            {"INFO"},
            (
                f"Removed {totals[0]} modifiers, {totals[1]} constraints, "
                f"{totals[2]} drivers, {totals[3]} custom properties, "
                f"{totals[4]} shape keys from {len(selected)} object(s)."
            ),
        )
        return {"FINISHED"}


class CLEAN_OT_mute_selected_drivers(bpy.types.Operator):
    bl_idname = "clean.mute_selected_drivers"
    bl_label = "Mute Selected Drivers"
    bl_description = "Mute or unmute drivers on selected armatures without deleting them"
    bl_options = {"REGISTER", "UNDO"}

    target: bpy.props.EnumProperty(
        name="Driver Target",
        items=[
            ("ARMATURE", "Armature Drivers", "Mute armature object/data drivers, excluding pose-bone drivers"),
            ("BONE", "Bone Drivers", "Mute pose-bone drivers on selected armatures"),
            ("BOTH", "Armature and Bone Drivers", "Mute armature object/data and pose-bone drivers"),
        ],
        default="BOTH",
    )

    mute: bpy.props.BoolProperty(
        name="Mute",
        default=True,
    )

    def execute(self, context):
        armatures = [obj for obj in context.selected_objects if obj.type == "ARMATURE"]
        if not armatures:
            self.report({"WARNING"}, "No selected armatures.")
            return {"CANCELLED"}

        changed_armature = 0
        changed_bone = 0
        for armature in armatures:
            if self.target in {"ARMATURE", "BOTH"}:
                changed_armature += set_armature_driver_mute(armature, self.mute)
            if self.target in {"BONE", "BOTH"}:
                changed_bone += set_bone_driver_mute(armature, self.mute)

        action = "Muted" if self.mute else "Unmuted"
        self.report(
            {"INFO"},
            (
                f"{action} {changed_armature} armature drivers and "
                f"{changed_bone} bone drivers on {len(armatures)} armature(s)."
            ),
        )
        return {"FINISHED"}


class CLEAN_PT_selection_cleaner(bpy.types.Panel):
    bl_label = "Selection Cleaner"
    bl_idname = "CLEAN_PT_selection_cleaner"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Clean"

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)

        op = col.operator("clean.delete_selected", text="Delete Modifiers")
        op.cleanup_type = "MODIFIERS"

        op = col.operator("clean.delete_selected", text="Delete Constraints")
        op.cleanup_type = "CONSTRAINTS"

        op = col.operator("clean.delete_selected", text="Delete Drivers")
        op.cleanup_type = "DRIVERS"

        op = col.operator("clean.delete_selected", text="Delete Custom Properties")
        op.cleanup_type = "CUSTOM_PROPERTIES"

        op = col.operator("clean.delete_selected", text="Delete Shape Keys")
        op.cleanup_type = "SHAPE_KEYS"

        col.separator()

        op = col.operator("clean.delete_selected", text="Delete All")
        op.cleanup_type = "ALL"

        if has_selected_armature_bones(context):
            layout.separator()
            bone_col = layout.column(align=True)

            op = bone_col.operator("clean.delete_selected", text="Delete Bone Constraints")
            op.cleanup_type = "BONE_CONSTRAINTS"

            op = bone_col.operator("clean.delete_selected", text="Delete Bone Drivers")
            op.cleanup_type = "BONE_DRIVERS"

            op = bone_col.operator("clean.delete_selected", text="Delete Bone Custom Properties")
            op.cleanup_type = "BONE_CUSTOM_PROPERTIES"

            bone_col.separator()

            op = bone_col.operator("clean.mute_selected_drivers", text="Mute Armature Drivers")
            op.target = "ARMATURE"
            op.mute = True

            op = bone_col.operator("clean.mute_selected_drivers", text="Unmute Armature Drivers")
            op.target = "ARMATURE"
            op.mute = False

            op = bone_col.operator("clean.mute_selected_drivers", text="Mute Bone Drivers")
            op.target = "BONE"
            op.mute = True

            op = bone_col.operator("clean.mute_selected_drivers", text="Unmute Bone Drivers")
            op.target = "BONE"
            op.mute = False

            op = bone_col.operator("clean.mute_selected_drivers", text="Mute Armature + Bone Drivers")
            op.target = "BOTH"
            op.mute = True

            op = bone_col.operator("clean.mute_selected_drivers", text="Unmute Armature + Bone Drivers")
            op.target = "BOTH"
            op.mute = False

            bone_col.separator()

            op = bone_col.operator("clean.delete_selected", text="Delete All Bone Data")
            op.cleanup_type = "BONE_ALL"


classes = (
    CLEAN_OT_delete_selected,
    CLEAN_OT_mute_selected_drivers,
    CLEAN_PT_selection_cleaner,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


def main():
    selected = list(bpy.context.selected_objects)
    if not selected:
        print("No objects selected.")
        return

    totals = [0, 0, 0, 0, 0]
    for obj in selected:
        removed = clear_object_stack(obj)
        totals = [a + b for a, b in zip(totals, removed)]
        print(
            f"{obj.name}: removed {removed[0]} modifiers, "
            f"{removed[1]} constraints, {removed[2]} drivers, "
            f"{removed[3]} custom properties, {removed[4]} shape keys."
        )

    print(
        f"Done. Removed {totals[0]} modifiers, "
        f"{totals[1]} constraints, {totals[2]} drivers, "
        f"{totals[3]} custom properties, {totals[4]} shape keys from {len(selected)} object(s)."
    )


if __name__ == "__main__":
    register()
