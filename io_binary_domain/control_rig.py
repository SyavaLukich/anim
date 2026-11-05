# SPDX-License-Identifier: GPL-2.0-or-later
"""Non-destructive animator rig. Blender IK, not game solver emulation.

All controls/mechanisms live in a separate armature. Original deform bones,
rest data, skinning, mesh parenting and source Actions are never replaced.
"""
import json
import math
import uuid
import bpy
from mathutils import Vector, Matrix
from bpy.props import BoolProperty, EnumProperty, IntProperty

PREFIX = 'BD Live | '
LIMBS = {f'{part}_{side}': ([f'asi{i}_{side}_n' for i in (1,2,3)] if part == 'leg'
                           else [f'ude{i}_{side}_n' for i in (1,2,3)])
         for part in ('leg','arm') for side in ('l','r')}


def source_and_control(obj):
    if obj is None or obj.type != 'ARMATURE':
        return None, None
    if obj.get('bd_control_kind') == 'animator_v1':
        return obj.get('bd_source'), obj
    return obj, obj.get('bd_control_rig')


def refresh(ctrl):
    ctrl.update_tag(refresh={'OBJECT'})
    bpy.context.view_layer.update()


def driver(constraint, ctrl, prop, factor=1):
    d = constraint.driver_add('influence').driver
    d.type = 'SCRIPTED'; d.expression = 'value' if factor == 1 else f'value*{factor}'
    v = d.variables.new(); v.name = 'value'; v.type = 'SINGLE_PROP'
    v.targets[0].id = ctrl; v.targets[0].data_path = f'["{prop}"]'


def prop(obj, name, value, lo=0, hi=1, description=''):
    obj[name] = value
    obj.id_properties_ui(name).update(min=lo, max=hi, soft_min=lo, soft_max=hi,
                                     description=description)


def _copy(pb, target, subtarget, ctrl, property_name, factor=1, rotation_only=False):
    c = pb.constraints.new('COPY_ROTATION' if rotation_only else 'COPY_TRANSFORMS')
    c.name = PREFIX+subtarget
    c.target = target; c.subtarget = subtarget
    c.owner_space = c.target_space = 'WORLD'
    driver(c, ctrl, property_name, factor)
    return c


def _new_bone(data, name, head, tail, parent=None, matrix=None):
    b = data.edit_bones.new(name)
    b.head, b.tail = head, tail
    if (b.tail-b.head).length < 1e-5:
        b.tail = b.head+Vector((0,.05,0))
    if matrix is not None:
        b.align_roll(matrix.to_3x3().col[2])
    b.use_deform = False
    b.parent = data.edit_bones.get(parent) if parent else None
    return b


def _pole(a,b,c,size):
    axis = c-a
    bend = b-a-axis*((b-a).dot(axis)/max(axis.length_squared,1e-12))
    if bend.length < 1e-5:
        bend = Vector((0,0,1))
        if abs(axis.normalized().dot(bend)) > .9:
            bend = Vector((1,0,0))
        bend -= axis.normalized()*bend.dot(axis.normalized())
    return b+bend.normalized()*size


def _pole_angle(ctrl, limb):
    """Calibrate the pole convention from the mechanism's rest frame."""
    a,b = (ctrl.data.bones[f'MCH_{limb}_{i}'] for i in (0,1))
    pole = ctrl.data.bones['CTRL_pole_'+limb].head_local
    axis = b.tail_local-a.head_local
    normal = axis.cross(pole-a.head_local)
    projected = normal.cross(axis)
    x = a.matrix_local.to_3x3().col[0]
    angle = x.angle(projected)
    if x.cross(projected).dot(a.tail_local-a.head_local) < 0:
        angle = -angle
    return angle


def _widget(ctrl, label, kind):
    n = 24
    if kind == 'diamond':
        vertices = [(0,0,.8),(.6,0,0),(0,0,-.8),(-.6,0,0)]
        edges = [(i,(i+1)%4) for i in range(4)]
    else:
        vertices = [(math.cos(i*2*math.pi/n),0,math.sin(i*2*math.pi/n)) for i in range(n)]
        edges = [(i,(i+1)%n) for i in range(n)]
    mesh = bpy.data.meshes.new('BD widget '+label); mesh.from_pydata(vertices,edges,[])
    ob = bpy.data.objects.new('BD widget '+ctrl.name+' '+label,mesh)
    bpy.context.scene.collection.objects.link(ob)
    ob['bd_widget_owner'] = ctrl['bd_uid']; ob.hide_render=True; ob.hide_set(True)
    return ob


def create(source, ik_default=False):
    if source is None or source.type != 'ARMATURE' or not source.get('gmd_source'):
        raise ValueError('Select an imported GMD armature')
    if source.get('bd_control_rig'):
        raise ValueError('A control rig already exists; select it or remove it first')
    missing = [n for names in LIMBS.values() for n in names if n not in source.data.bones]
    if missing or 'center_c_n' not in source.data.bones:
        raise ValueError('Full human skeleton required: '+', '.join(missing))
    if any(not c.mute for pb in source.pose.bones for c in pb.constraints):
        raise ValueError('Mute existing deform constraints before creating controls')
    if source.animation_data and (any(not d.mute for d in source.animation_data.drivers) or
                                  any(not t.mute and len(t.strips) for t in source.animation_data.nla_tracks)):
        raise ValueError('Mute original drivers/NLA before creating controls')
    if any(n.startswith(('CTRL_','MCH_','OUT_')) for n in source.data.bones.keys()):
        raise ValueError('Reserved control bone names already exist')
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.context.view_layer.update()
    initial = {pb.name: pb.matrix.copy() for pb in source.pose.bones}
    bases = {pb.name: pb.matrix_basis.copy() for pb in source.pose.bones}
    data = bpy.data.armatures.new(source.name+' controls')
    ctrl = bpy.data.objects.new(source.name+' Controls', data)
    bpy.context.collection.objects.link(ctrl)
    ctrl.matrix_world = source.matrix_world.copy(); ctrl.show_in_front=True
    # Follow later Object Mode moves of the imported armature.  Pose bones do
    # not feed the source object's own transform, so this is not a cycle.
    ctrl.parent = source
    ctrl.matrix_parent_inverse = Matrix.Identity(4)
    ctrl.matrix_basis = Matrix.Identity(4)
    ctrl['bd_control_kind']='animator_v1'; ctrl['bd_source']=source; ctrl['bd_uid']=uuid.uuid4().hex
    prop(ctrl,'bd_enabled',1.0,description='Follow animator rig; zero restores original Action')
    prop(ctrl,'bd_helpers',0.0,description='Approximate twist/bend sharing; not game OPE0')
    for limb in LIMBS: prop(ctrl,'bd_ik_'+limb,0.0,description='0: FK / 1: IK (use Match buttons to avoid jumps)')
    size = max(.1, abs(source.data.bones['center_c_n'].head_local.y))
    created_action = None
    try:
        for ob in bpy.context.selected_objects: ob.select_set(False)
        ctrl.select_set(True); bpy.context.view_layer.objects.active=ctrl
        bpy.ops.object.mode_set(mode='EDIT')
        _new_bone(data,'CTRL_global',Vector((0,0,0)),Vector((0,size*.2,0)))
        center=source.data.bones['center_c_n'].head_local.copy()
        _new_bone(data,'CTRL_body',center,center+Vector((0,size*.15,0)),'CTRL_global')
        for original in source.data.bones:
            _new_bone(data,original.name,original.head_local,original.tail_local,
                      original.parent.name if original.parent else 'CTRL_body',original.matrix_local)
        for limb,names in LIMBS.items():
            a,b,c = (source.data.bones[n] for n in names)
            rootparent = a.parent.name if a.parent else 'CTRL_body'
            for i,(first,last) in enumerate(((a,b),(b,c))):
                mechanism = _new_bone(data,f'MCH_{limb}_{i}',first.head_local,last.head_local,
                          rootparent if i==0 else f'MCH_{limb}_0',first.matrix_local)
                if i == 1:
                    # Blender's IK solver expects a genuinely connected
                    # two-bone chain; the GMD clone itself stays disconnected.
                    mechanism.use_connect = True
                _new_bone(data,f'OUT_{limb}_{i}',first.head_local,first.tail_local,
                          f'MCH_{limb}_{i}',first.matrix_local)
            # A location-only end frame on the solved lower segment prevents
            # COPY_TRANSFORMS-to-goal from stretching an unreachable limb.
            end_frame = _new_bone(data,f'OUT_{limb}_2',c.head_local,c.tail_local,
                      f'MCH_{limb}_1',c.matrix_local)
            end_frame.use_connect = True
            name='CTRL_'+limb
            _new_bone(data,name,c.head_local,c.head_local+Vector((0,size*.12,0)),'CTRL_global')
            goalparent=name
            if limb.startswith('leg'):
                toe = source.data.bones.get('asi4_'+limb[-1]+'_n')
                heel=Vector((c.head_local.x,0,c.head_local.z-size*.08))
                tip=toe.head_local.copy() if toe else c.head_local+Vector((0,0,size*.14))
                tip.y=0
                for pivot,point in (('heel',heel),('toe',tip)):
                    pname='MCH_'+limb+'_'+pivot
                    _new_bone(data,pname,point,point+Vector((0,size*.08,0)),goalparent)
                    goalparent=pname
            _new_bone(data,'MCH_goal_'+limb,c.head_local,c.tail_local,goalparent,c.matrix_local)
            pole=_pole(a.head_local,b.head_local,c.head_local,size*.5)
            _new_bone(data,'CTRL_pole_'+limb,pole,pole+Vector((0,size*.1,0)),'CTRL_global')
        # Driver bind frames for optional helper sharing; no evaluated back-reference.
        from . import mmo
        from pathlib import Path
        rig=mmo.parse_moma((Path(__file__).parent/'rigs/p_man.mma').read_bytes())
        helper_drivers={}
        for op in rig['operations']:
            name=rig['nodes'][op['target']]
            drivers=[rig['nodes'][i] for i in op['drivers'] if i>=0]
            if (name not in source.data.bones or len(drivers)<2 or
                    not any(x in name for x in ('twist','elbow','knee'))): continue
            if any(d not in source.data.bones for d in drivers[-2:]): continue
            helper_drivers[name]=drivers[-2:]
            for i,parent in enumerate(drivers[-2:]):
                original=source.data.bones[name]
                _new_bone(data,f'MCH_share_{i}_{name}',original.head_local,original.tail_local,parent,original.matrix_local)
        bpy.ops.object.mode_set(mode='POSE')
        controls=data.collections.new('BD Controls')
        fk=data.collections.new('BD FK (original names)')
        mechanisms=data.collections.new('BD Mechanisms')
        for bone in data.bones:
            (controls if bone.name.startswith('CTRL_') else mechanisms if bone.name.startswith(('MCH_','OUT_')) else fk).assign(bone)
        mechanisms.is_visible=False; fk.is_visible=False
        for name,basis in bases.items(): ctrl.pose.bones[name].matrix_basis=basis
        if source.animation_data and source.animation_data.action:
            from . import _assign_momo_action
            created_action=source.animation_data.action.copy(); created_action.name += '__controls'
            created_action.use_fake_user=True; created_action['bd_control_action']=True
            _assign_momo_action(ctrl,created_action)
        for limb,names in LIMBS.items():
            ik=ctrl.pose.bones['MCH_'+limb+'_1'].constraints.new('IK')
            ik.name=PREFIX+'IK '+limb; ik.target=ctrl; ik.subtarget='MCH_goal_'+limb
            ik.pole_target=ctrl; ik.pole_subtarget='CTRL_pole_'+limb
            ik.chain_count=2; ik.use_tail=True; ik.use_stretch=False
            ik.pole_angle=_pole_angle(ctrl,limb)
            for i in (0,1):
                ctrl.pose.bones[f'MCH_{limb}_{i}'].ik_stretch=0
                _copy(ctrl.pose.bones[names[i]],ctrl,f'OUT_{limb}_{i}',ctrl,'bd_ik_'+limb)
            endpoint = ctrl.pose.bones[names[2]]
            location = endpoint.constraints.new('COPY_LOCATION')
            location.name = PREFIX+'IK position '+limb
            location.target = ctrl; location.subtarget = f'OUT_{limb}_2'
            location.owner_space = location.target_space = 'WORLD'
            driver(location, ctrl, 'bd_ik_'+limb)
            _copy(endpoint, ctrl, 'MCH_goal_'+limb, ctrl, 'bd_ik_'+limb,
                  rotation_only=True)
            if limb.startswith('leg'):
                foot=ctrl.pose.bones['CTRL_'+limb]
                prop(foot,'roll',0.0,-1.3,1.3,'Foot roll in radians: negative heel / positive toe')
                for pivot,expr in (('heel','min(roll,0)'),('toe','max(roll,0)')):
                    pb=ctrl.pose.bones[f'MCH_{limb}_{pivot}']; pb.rotation_mode='XYZ'
                    d=pb.driver_add('rotation_euler',0).driver; d.type='SCRIPTED'; d.expression=expr
                    v=d.variables.new(); v.name='roll'; v.type='SINGLE_PROP'; v.targets[0].id=ctrl
                    v.targets[0].data_path=foot.path_from_id()+'["roll"]'
        for name in helper_drivers:
            for i in (0,1):
                _copy(ctrl.pose.bones[name],ctrl,f'MCH_share_{i}_{name}',ctrl,'bd_helpers',1 if i==0 else .5,rotation_only=True)
        circle=_widget(ctrl,'circle','circle'); diamond=_widget(ctrl,'pole','diamond')
        for pb in ctrl.pose.bones:
            if not pb.name.startswith('CTRL_'): continue
            pb.custom_shape=diamond if 'pole' in pb.name else circle
            pb.use_custom_shape_bone_size=False
            radius=size*(.35 if pb.name=='CTRL_global' else .18 if pb.name=='CTRL_body' else .07)
            pb.custom_shape_scale_xyz=(radius,)*3
            pb.bone.color.palette='THEME09' if 'pole' in pb.name else 'THEME04' if 'arm' in pb.name else 'THEME03'
        refresh(ctrl)
        for limb in LIMBS:
            match(ctrl,limb,'IK',False,enable=ik_default)
        # Create followers last, so failures cannot strand the original skeleton.
        for pb in source.pose.bones:
            c=pb.constraints.new('COPY_TRANSFORMS'); c.name=PREFIX+'follow'
            c.target=ctrl; c.subtarget=pb.name; c.owner_space=c.target_space='WORLD'
            driver(c,ctrl,'bd_enabled')
        source['bd_control_rig']=ctrl
        ctrl['bd_source_bones']=json.dumps(list(source.data.bones.keys()))
        refresh(ctrl)
        return ctrl
    except Exception:
        if bpy.context.object and bpy.context.object.mode!='OBJECT': bpy.ops.object.mode_set(mode='OBJECT')
        for pb in source.pose.bones:
            for c in list(pb.constraints):
                if c.name.startswith(PREFIX) and c.target==ctrl: pb.constraints.remove(c)
        uid=ctrl.get('bd_uid'); bpy.data.objects.remove(ctrl,do_unlink=True)
        for ob in list(bpy.data.objects):
            if ob.get('bd_widget_owner')==uid:
                mesh=ob.data; bpy.data.objects.remove(ob,do_unlink=True)
                if mesh.users==0: bpy.data.meshes.remove(mesh)
        if data.users==0: bpy.data.armatures.remove(data)
        if created_action and created_action.users<=1: bpy.data.actions.remove(created_action)
        raise


def _calibrate_pole(ctrl, limb, desired, endpoint):
    """Newton-calibrate pole_angle so the solved joint lands on the FK joint.

    Blender's pole convention (which local axis, which sign) depends on the
    GMD bone rolls, so measure the solver's actual sensitivity instead of
    assuming a formula.  One Newton step is normally exact: changing the
    pole angle rigidly rotates the solved joint around the root-goal axis.
    """
    ik = ctrl.pose.bones['MCH_'+limb+'_1'].constraints[0]
    root = ctrl.pose.bones['MCH_'+limb+'_0'].head.copy()
    axis = endpoint-root
    if axis.length < 1e-9:
        return
    axis.normalize()
    wanted = desired-root
    wanted -= axis*wanted.dot(axis)
    if wanted.length < 1e-7:
        return  # straight FK limb: every bend plane reaches the goal

    def signed_error():
        refresh(ctrl)
        joint = ctrl.pose.bones['MCH_'+limb+'_1'].head.copy()
        v = joint-root
        v -= axis*v.dot(axis)
        if v.length < 1e-9:
            return None
        return math.atan2(axis.dot(v.cross(wanted)), v.dot(wanted))

    error = signed_error()
    if error is None or abs(error) < 1e-6:
        return
    ik.pole_angle += 0.1
    probed = signed_error()
    if probed is None:
        ik.pole_angle -= 0.1
        return
    sensitivity = (probed-error)/0.1
    if abs(sensitivity) < 1e-6:
        ik.pole_angle -= 0.1
        refresh(ctrl)
        return
    ik.pole_angle -= probed/sensitivity
    for _ in range(3):
        error = signed_error()
        if error is None or abs(error) < 1e-6:
            break
        ik.pole_angle -= error/sensitivity
    refresh(ctrl)

def _segment_matrix(start, end, reference):
    """World frame of one mechanism segment: Y along the segment, roll from
    the corresponding FK bone frame."""
    y = end-start
    if y.length < 1e-9:
        return reference.copy()
    y.normalize()
    z = reference.to_3x3().col[2].copy()
    z -= y*z.dot(y)
    if z.length < 1e-6:
        z = reference.to_3x3().col[0].copy()
        z -= y*z.dot(y)
    z.normalize()
    x = y.cross(z)
    result = Matrix(((x[0], y[0], z[0]), (x[1], y[1], z[1]), (x[2], y[2], z[2]))).to_4x4()
    result.translation = start
    return result


def _position_ik_controls(ctrl, limb, matrices, key=True):
    """Place goal, pole and axial compensation frames on an FK limb pose."""
    control = ctrl.pose.bones['CTRL_'+limb]
    if limb.startswith('leg'):
        control['roll'] = 0.0
    refresh(ctrl)
    # The goal is a descendant of the hand/foot control.  Preserve its
    # current control-local offset (notably the heel/toe pivot chain), then
    # place the control so the goal lands on the evaluated FK endpoint.
    goal = ctrl.pose.bones['MCH_goal_'+limb].matrix
    offset = control.matrix.inverted_safe() @ goal
    control.matrix = matrices[2] @ offset.inverted_safe()
    pole = ctrl.pose.bones['CTRL_pole_'+limb]
    pole_matrix = pole.matrix.copy()
    size = max(.1, abs(ctrl.data.bones['center_c_n'].head_local.y))
    pole_matrix.translation = _pole(*(m.translation for m in matrices), size*.5)
    pole.matrix = pole_matrix
    refresh(ctrl)
    # GMD bind chains are perfectly straight, which is a singularity for
    # Blender's pole-target IK: it then refuses to bend.  Pose the mechanism
    # segments onto the FK solution first, breaking the singularity on the
    # correct (pole) side; the live solver starts from this basis.
    joints = [m.translation for m in matrices]
    for i in (0, 1):
        ctrl.pose.bones[f'MCH_{limb}_{i}'].matrix = _segment_matrix(
            joints[i], joints[i+1], matrices[i])
        refresh(ctrl)
    _calibrate_pole(ctrl, limb, matrices[1].translation, matrices[2].translation)
    # Pole direction fixes the two joint positions, but not necessarily the
    # axial frame of either segment.  The OUT bones absorb that difference.
    for i in (0, 1):
        out = ctrl.pose.bones[f'OUT_{limb}_{i}']
        # Keep the output frame exactly on the solved mechanism joint.  Only
        # its axial orientation is compensated; absorbing a positional error
        # here would let the displayed segment lengths drift during motion.
        location, rotation, scale = matrices[i].decompose()
        out.matrix = Matrix.LocRotScale(out.head.copy(), rotation, scale)
        refresh(ctrl)
        if key:
            out.rotation_mode = 'QUATERNION'
            for path in ('location', 'rotation_quaternion', 'scale'):
                out.keyframe_insert(data_path=path)
    if key:
        # The solver starts from the mechanism bases, so they are part of
        # the converted pose; leaving them at the last frame's values would
        # skew the OUT twist compensation on other frames.
        for i in (0, 1):
            mechanism = ctrl.pose.bones[f'MCH_{limb}_{i}']
            mechanism.rotation_mode = 'QUATERNION'
            for path in ('location', 'rotation_quaternion', 'scale'):
                mechanism.keyframe_insert(data_path=path)
        ctrl.pose.bones['MCH_'+limb+'_1'].constraints[0].keyframe_insert(
            data_path='pole_angle')
        for pb in (control, pole):
            pb.rotation_mode = 'QUATERNION'
            for path in ('location', 'rotation_quaternion', 'scale'):
                pb.keyframe_insert(data_path=path)
        if limb.startswith('leg'):
            control.keyframe_insert(data_path='["roll"]')


def _key_previous_switch(ctrl, limb, direction):
    """Keep the old IK/FK state up to the frame before a keyed switch."""
    scene = bpy.context.scene
    frame = scene.frame_current
    action = ctrl.animation_data.action if ctrl.animation_data else None
    start = int(action.frame_range[0]) if action else frame
    if frame <= start:
        return
    prop_name = 'bd_ik_' + limb
    ctrl[prop_name] = 0.0 if direction == 'IK' else 1.0
    ctrl.keyframe_insert(data_path=f'["{prop_name}"]', frame=frame-1)


def match(ctrl,limb,direction,key=True,enable=True):
    names=LIMBS[limb]; refresh(ctrl)
    matrices=[ctrl.pose.bones[n].matrix.copy() for n in names]
    if key:
        _key_previous_switch(ctrl, limb, direction)
    if direction=='IK':
        _position_ik_controls(ctrl, limb, matrices, key=key)
        ctrl['bd_ik_'+limb]=1.0 if enable else 0.0
    else:
        ctrl['bd_ik_'+limb]=0.0; refresh(ctrl)
        for name,mat in zip(names,matrices):
            pb=ctrl.pose.bones[name]; pb.matrix=mat; refresh(ctrl)
            if key:
                pb.rotation_mode='QUATERNION'
                for path in ('location','rotation_quaternion','scale'): pb.keyframe_insert(data_path=path)
    if key: ctrl.keyframe_insert(data_path='["bd_ik_'+limb+'"]')
    refresh(ctrl)


def sync_action(source):
    """Copy the source object's current Action onto the FK control bones."""
    ctrl = source.get('bd_control_rig')
    if not ctrl:
        raise ValueError('Create a control rig first')
    if not source.animation_data or not source.animation_data.action:
        raise ValueError('The original skeleton has no current Action to copy')
    from . import _assign_momo_action
    copied = source.animation_data.action.copy()
    copied.name += '__controls'
    copied.use_fake_user = True
    copied['bd_control_action'] = True
    _assign_momo_action(ctrl, copied)
    # A copied clip is, by definition, FK data on the same-named bones.
    # Mechanisms return to bind offsets; Match/Convert can arm IK again.
    for pb in ctrl.pose.bones:
        if pb.name.startswith(('CTRL_', 'MCH_', 'OUT_')):
            pb.matrix_basis.identity()
            if pb.name.startswith('CTRL_leg_'):
                pb['roll'] = 0.0
    for limb in LIMBS:
        ctrl['bd_ik_'+limb] = 0.0
    ctrl['bd_enabled'] = 1.0
    refresh(ctrl)
    for limb in LIMBS:
        match(ctrl, limb, 'IK', key=False, enable=False)
    return copied


def convert_to_ik(ctrl, start, end, limbs=None):
    """Key IK goals/poles from the copied FK clip over an inclusive range."""
    limbs = tuple(limbs or LIMBS)
    if end < start or end-start > 2000:
        raise ValueError('Choose a valid conversion range of at most 2001 frames')
    unknown = [name for name in limbs if name not in LIMBS]
    if unknown:
        raise ValueError('Unknown limb: '+', '.join(unknown))
    scene = bpy.context.scene
    old_frame, old_subframe = scene.frame_current, scene.frame_subframe
    try:
        for limb in limbs:
            ctrl['bd_ik_'+limb] = 0.0
        refresh(ctrl)
        for limb in limbs:
            names = LIMBS[limb]
            for frame in range(start, end+1):
                scene.frame_set(frame)
                matrices = [ctrl.pose.bones[n].matrix.copy() for n in names]
                _position_ik_controls(ctrl, limb, matrices, key=True)
            ctrl['bd_ik_'+limb] = 1.0
            ctrl.keyframe_insert(data_path=f'["bd_ik_{limb}"]', frame=start)
            if end > start:
                ctrl.keyframe_insert(data_path=f'["bd_ik_{limb}"]', frame=end)
        # Dense converted keys must not acquire Bezier overshoot between
        # frames; the FK clip they came from is reproduced at the keys.
        action = ctrl.animation_data.action if ctrl.animation_data else None
        if action is not None:
            if hasattr(action, 'slots') and len(action.slots) and len(action.layers):
                curves = action.layers[0].strips[0].channelbag(action.slots[0]).fcurves
            else:
                curves = action.fcurves
            for curve in curves:
                if any(tag in curve.data_path
                       for tag in ('CTRL_', 'OUT_', 'MCH_', 'bd_ik')):
                    for point in curve.keyframe_points:
                        if start <= point.co[0] <= end:
                            point.interpolation = 'LINEAR'
        refresh(ctrl)
    finally:
        scene.frame_set(old_frame, subframe=old_subframe)
        refresh(ctrl)

def remove(source):
    ctrl=source.get('bd_control_rig')
    if not ctrl: return
    if bpy.context.object and bpy.context.object.mode!='OBJECT': bpy.ops.object.mode_set(mode='OBJECT')
    for pb in source.pose.bones:
        for c in list(pb.constraints):
            if c.name.startswith(PREFIX) and getattr(c,'target',None)==ctrl:
                c.driver_remove('influence'); pb.constraints.remove(c)
    uid=ctrl.get('bd_uid'); data=ctrl.data
    del source['bd_control_rig']
    bpy.data.objects.remove(ctrl,do_unlink=True)
    for ob in list(bpy.data.objects):
        if ob.get('bd_widget_owner')==uid:
            mesh=ob.data; bpy.data.objects.remove(ob,do_unlink=True)
            if mesh.users==0: bpy.data.meshes.remove(mesh)
    if data.users==0: bpy.data.armatures.remove(data)
    source.select_set(True); bpy.context.view_layer.objects.active=source
    bpy.context.view_layer.update()


def bake(source,start,end):
    from array import array
    from . import _assign_momo_action, _key_momo_basis, _fill_body_motion_curves
    ctrl=source.get('bd_control_rig')
    if not ctrl or not ctrl.get('bd_enabled'): raise ValueError('Enable the control rig before baking')
    if end<start or end-start>10000: raise ValueError('Choose a valid range of at most 10001 frames')
    scene=bpy.context.scene; oldframe,sub=scene.frame_current,scene.frame_subframe
    original=source.animation_data.action if source.animation_data else None
    samples={}; previous={}; result=None
    try:
        for frame in range(start,end+1):
            scene.frame_set(frame); bpy.context.view_layer.update()
            for pb in source.pose.bones:
                b=pb.bone; p=pb.parent
                kwargs=dict(parent_matrix=p.matrix,parent_matrix_local=p.bone.matrix_local) if p else {}
                basis=b.convert_local_to_pose(pb.matrix,b.matrix_local,invert=True,**kwargs)
                loc,q,scale=basis.decompose()
                if pb.name in previous and q.dot(previous[pb.name])<0: q.negate()
                previous[pb.name]=q.copy()
                for propname,values in (('location',loc),('rotation_quaternion',q),('scale',scale)):
                    for i,v in enumerate(values):
                        if not math.isfinite(v): raise ValueError('Non-finite rig pose')
                        samples.setdefault((pb.path_from_id(propname),i),array('f')).extend((frame,v))
        result=bpy.data.actions.new(source.name+'__control_bake'); result.use_fake_user=True
        result['bd_control_bake']=True; result['bd_frames']=[start,end]
        ctrl['bd_enabled']=0.0; refresh(ctrl)
        _assign_momo_action(source,result)
        for pb in source.pose.bones: _key_momo_basis(pb,pb.matrix_basis.copy(),start)
        _fill_body_motion_curves(result,samples)
        result.frame_range=(start,end)
        if original: original.use_fake_user=True
        return result
    except Exception:
        _assign_momo_action(source,original); ctrl['bd_enabled']=1.0
        if result: bpy.data.actions.remove(result)
        raise
    finally:
        scene.frame_set(oldframe,subframe=sub); refresh(ctrl)


class BDOMAIN_OT_create_controls(bpy.types.Operator):
    bl_idname='bdomain.create_controls'
    bl_label='Create animator control rig'
    bl_options={'REGISTER','UNDO'}
    ik_default: BoolProperty(
        name='Start limbs in IK (current frame only)', default=False,
        description='Off preserves an animated clip in FK; use Match IK or Convert to key IK controllers')
    def invoke(self,context,event):
        ob = context.object
        action = ob.animation_data.action if ob and ob.animation_data else None
        # An animated clip starts in FK so playback is unchanged.  A static
        # pose can start directly in IK.
        self.ik_default = not (action and action.frame_range[1] > action.frame_range[0])
        return context.window_manager.invoke_props_dialog(self)
    def execute(self,context):
        try: create(context.object,self.ik_default)
        except Exception as exc:
            self.report({'ERROR'},str(exc)); return {'CANCELLED'}
        self.report({'INFO'},'Separate control rig created; deform skeleton and source Actions kept')
        return {'FINISHED'}


class BDOMAIN_OT_match_controls(bpy.types.Operator):
    bl_idname='bdomain.match_controls'
    bl_label='Match and switch IK/FK'
    bl_options={'REGISTER','UNDO'}
    limb: EnumProperty(items=[(n,n.replace('_',' ').title(),'') for n in LIMBS])
    direction: EnumProperty(items=[('IK','To IK','Match controllers to current limb'),('FK','To FK','Match FK bones to current IK pose')])
    key: BoolProperty(name='Insert switch keys',default=True)
    def execute(self,context):
        source,ctrl=source_and_control(context.object)
        if not ctrl: return {'CANCELLED'}
        try: match(ctrl,self.limb,self.direction,self.key)
        except Exception as exc:
            self.report({'ERROR'},str(exc)); return {'CANCELLED'}
        return {'FINISHED'}


class BDOMAIN_OT_sync_controls(bpy.types.Operator):
    bl_idname='bdomain.sync_controls'
    bl_label='Copy source Action to FK controls'
    bl_options={'REGISTER','UNDO'}
    def invoke(self,context,event): return context.window_manager.invoke_confirm(self,event)
    def execute(self,context):
        source,ctrl=source_and_control(context.object)
        if not source: return {'CANCELLED'}
        try: sync_action(source)
        except Exception as exc:
            self.report({'ERROR'},str(exc)); return {'CANCELLED'}
        self.report({'INFO'},'Source Action copied to same-name FK controls; limbs set to FK')
        return {'FINISHED'}


class BDOMAIN_OT_convert_controls(bpy.types.Operator):
    bl_idname='bdomain.convert_controls_to_ik'
    bl_label='Convert FK clip to IK keys'
    bl_options={'REGISTER','UNDO'}
    start: IntProperty(name='Start frame',default=1)
    end: IntProperty(name='End frame',default=30)
    def invoke(self,context,event):
        source,ctrl=source_and_control(context.object)
        action=ctrl.animation_data.action if ctrl and ctrl.animation_data else None
        if action:
            self.start,self.end=(int(v) for v in action.frame_range)
        else:
            self.start,self.end=context.scene.frame_start,context.scene.frame_end
        return context.window_manager.invoke_props_dialog(self)
    def execute(self,context):
        source,ctrl=source_and_control(context.object)
        if not ctrl: return {'CANCELLED'}
        try: convert_to_ik(ctrl,self.start,self.end)
        except Exception as exc:
            self.report({'ERROR'},str(exc)); return {'CANCELLED'}
        self.report({'INFO'},'FK clip keyed onto IK controllers for all four limbs')
        return {'FINISHED'}


class BDOMAIN_OT_remove_controls(bpy.types.Operator):
    bl_idname='bdomain.remove_controls'
    bl_label='Remove control rig'
    bl_options={'REGISTER','UNDO'}
    def invoke(self,context,event): return context.window_manager.invoke_confirm(self,event)
    def execute(self,context):
        source,ctrl=source_and_control(context.object)
        if source: remove(source)
        return {'FINISHED'}


class BDOMAIN_OT_bake_controls(bpy.types.Operator):
    bl_idname='bdomain.bake_controls'
    bl_label='Bake controls to original skeleton'
    bl_options={'REGISTER','UNDO'}
    start: IntProperty(name='Start frame',default=1)
    end: IntProperty(name='End frame',default=30)
    def invoke(self,context,event):
        self.start=context.scene.frame_start; self.end=context.scene.frame_end
        return context.window_manager.invoke_props_dialog(self)
    def execute(self,context):
        source,ctrl=source_and_control(context.object)
        try: bake(source,self.start,self.end)
        except Exception as exc:
            self.report({'ERROR'},str(exc)); return {'CANCELLED'}
        self.report({'INFO'},'Baked new Action; live rig disabled, source Action kept')
        return {'FINISHED'}


class BDOMAIN_OT_select_controls(bpy.types.Operator):
    bl_idname='bdomain.select_controls'
    bl_label='Select control rig'
    def execute(self,context):
        source,ctrl=source_and_control(context.object)
        if not ctrl: return {'CANCELLED'}
        if context.object.mode!='OBJECT': bpy.ops.object.mode_set(mode='OBJECT')
        for ob in context.selected_objects: ob.select_set(False)
        ctrl.select_set(True);context.view_layer.objects.active=ctrl
        bpy.ops.object.mode_set(mode='POSE')
        return {'FINISHED'}


def draw(layout,obj):
    source,ctrl=source_and_control(obj)
    if source is None or not source.get('gmd_source'): return
    box=layout.box();box.label(text='Animator controls (Blender IK)',icon='ARMATURE_DATA')
    if not ctrl:
        box.operator('bdomain.create_controls'); return
    box.prop(ctrl,'["bd_enabled"]',text='Live rig',slider=True)
    box.operator('bdomain.select_controls')
    row=box.row(align=True)
    row.operator('bdomain.sync_controls',text='Copy Action to FK',icon='ACTION')
    row.operator('bdomain.convert_controls_to_ik',text='Convert to IK keys',icon='CON_KINEMATIC')
    box.label(text='Animated clips start in FK; IK Match/Convert avoids pops')
    box.label(text='In IK, CTRL_body moves pelvis; feet stay planted')
    for limb in LIMBS:
        row=box.row(align=True);row.prop(ctrl,'["bd_ik_'+limb+'"]',text=limb.replace('_',' ').title(),slider=True)
        for direction in ('IK','FK'):
            op=row.operator('bdomain.match_controls',text='Match '+direction)
            op.limb=limb;op.direction=direction
    for side in ('l','r'):
        foot=ctrl.pose.bones['CTRL_leg_'+side]
        box.prop(foot,'["roll"]',text='Foot '+side.upper()+' roll (rad)')
    box.prop(ctrl,'["bd_helpers"]',text='Helper blend (approx.)',slider=True)
    fk=ctrl.data.collections.get('BD FK (original names)')
    if fk: box.prop(fk,'is_visible',text='Show FK bones')
    box.operator('bdomain.bake_controls',icon='ACTION')
    box.operator('bdomain.remove_controls',icon='TRASH')
    box.label(text='Match buttons key the switch at current frame')
    box.label(text='Not game IK/terrain collision')


CLASSES=(BDOMAIN_OT_create_controls,BDOMAIN_OT_match_controls,BDOMAIN_OT_sync_controls,
         BDOMAIN_OT_convert_controls,BDOMAIN_OT_remove_controls,
         BDOMAIN_OT_bake_controls,BDOMAIN_OT_select_controls)
