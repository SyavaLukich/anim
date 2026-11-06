# SPDX-License-Identifier: GPL-2.0-or-later
"""Non-destructive animator rig. Blender IK, not game solver emulation.

All controls/mechanisms live in a separate armature. Original deform bones,
rest data, skinning, mesh parenting and source Actions are never replaced.
"""
import json
import math
import uuid
import bpy
from mathutils import Vector, Matrix, Quaternion
from bpy.props import BoolProperty, EnumProperty, IntProperty

PREFIX = 'BD Live | '
# IK/FK chains: limb name -> clone bone names root..end.  The neck chain has
# only two real bones; its second segment ends at a virtual head-top point.
LIMBS = {f'{part}_{side}': ([f'asi{i}_{side}_n' for i in (1,2,3)] if part == 'leg'
                           else [f'ude{i}_{side}_n' for i in (1,2,3)])
         for part in ('leg','arm') for side in ('l','r')}
LIMBS['spine'] = ['kosi_c_n', 'mune_c_n', 'kubi_c_n']   # waist -> chest -> neck base
LIMBS['neck'] = ['kubi_c_n', 'face_c_n']                # neck -> head
KINDS = {name: (name.split('_')[0] if name not in ('spine', 'neck') else name)
         for name in LIMBS}
LABELS = {'leg_l': 'Leg L (hip-knee-ankle)', 'leg_r': 'Leg R (hip-knee-ankle)',
          'arm_l': 'Arm L (shoulder-elbow-wrist)', 'arm_r': 'Arm R (shoulder-elbow-wrist)',
          'spine': 'Spine (waist-chest)', 'neck': 'Neck-Head'}
# Anatomical bend side in armature-local space (Y up, character faces +Z),
# used when a chain is perfectly straight and has no bend to measure.
POLE_FALLBACK = {'leg': (0, 0, 1), 'arm': (0, 0, -1),
                 'spine': (0, 0, -1), 'neck': (0, 0, -1)}
# Per-kind IK joint limits in radians: (root, mid) flexion / side / twist.
LIMIT_PROFILES = {
    'leg':   {'flex': (2.2, 2.45), 'side': (1.20, 0.90), 'twist': (1.40, 1.20)},
    'arm':   {'flex': (2.6, 2.62), 'side': (1.40, 1.10), 'twist': (1.60, 1.40)},
    'spine': {'flex': (0.55, 0.55), 'side': (0.65, 0.65), 'twist': (0.90, 0.90)},
    'neck':  {'flex': (0.65, 0.55), 'side': (0.70, 0.65), 'twist': (1.00, 0.90)},
}
HYPER_EXTENSION = 0.12  # rad of allowed bend past straight, for every hinge
RIG_VERSION = 'v0.8.8'
# OPE0 IDs 11/12 target these helpers.  The supplied GMD's helper rest frames
# differ from asi2_*_n by about 26 degrees; a local COPY_ROTATION double-counts
# knee flexion in deep poses.  Leave them on their FK transforms until the
# game's code-specific OPE0 operators are reproduced rather than guessed.
HELPER_SHARE_EXCLUDED = frozenset(('asi2_l_sup', 'asi2_r_sup'))
# Clavicle/shoulder controls: single-bone drivers for kata_*_n.
SHOULDERS = {'shoulder_l': 'kata_l_n', 'shoulder_r': 'kata_r_n'}


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


def _pole(a,b,c,size,fallback=(0,0,1)):
    axis = c-a
    bend = b-a-axis*((b-a).dot(axis)/max(axis.length_squared,1e-12))
    if bend.length < 0.12*(b-a).length or bend.dot(Vector(fallback)) < 0:
        # Near-straight chain (a few degrees): the measured bend
        # direction is numerical noise and would put the pole on a
        # random side.  A measured bend on the opposite side of the
        # anatomical one is just as bad: at bind the chest sits slightly
        # in front of the waist-neck line, which would put the spine
        # pole in front and roll the whole torso.  Below ~7 degrees the
        # pole stays on the anatomical side; the goal alone decides the
        # bend direction.
        bend = Vector(fallback)
        if abs(axis.normalized().dot(bend)) > .9:
            bend = Vector((1,0,0))
        bend -= axis.normalized()*bend.dot(axis.normalized())
    return b+bend.normalized()*size


def _neck_end(matrix, bone):
    """Current head-top point: a virtual joint above face_c_n."""
    offset = (bone.tail_local-bone.head_local)
    if offset.length < 1e-9:
        offset = Vector((0, 0.12, 0))
    offset = offset.normalized()*0.12
    local = bone.matrix_local.to_3x3().inverted_safe() @ offset
    return matrix.translation + matrix.to_3x3() @ local


def _joint_matrices(ctrl, limb):
    """Evaluated root/mid/end matrices of a chain (virtual end for neck)."""
    names = LIMBS[limb]
    mats = [ctrl.pose.bones[n].matrix.copy() for n in names]
    if KINDS[limb] == 'neck':
        face = ctrl.pose.bones[names[1]]
        end = _neck_end(face.matrix, face.bone)
        mats.append(Matrix.LocRotScale(end, face.matrix.to_quaternion(),
                                       face.matrix.to_scale()))
    return mats


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


def _apply_limits(ctrl, limb, joints):
    """Calibrated IK joint limits on the two mechanism bones of a chain.

    Blender's IK limits are not Euler angles: the solver decomposes each
    segment rotation into a twist about its local Y axis plus a 2D swing
    vector (sin(theta/2) along local X/Z) and clamps those.  Fixed ranges
    around zero therefore reject legitimate GMD poses, whose bind frames
    carry roll/twist.  Instead each allowed interval is the anatomical
    range measured from straight, widened to include the mechanism's
    current (creation-time FK) parameters, so the creation pose always
    stays reachable.  Limits live on the mechanism only and are toggled
    by the driven bd_limits property; FK/Action playback is not clamped.
    """
    kind = KINDS[limb]
    profile = LIMIT_PROFILES[kind]
    axis = joints[2]-joints[0]
    if axis.length < 1e-9:
        return
    seg1 = joints[1]-joints[0]
    bend = seg1.copy()
    bend -= axis*(bend.dot(axis)/axis.length_squared)
    if (bend.length < 0.12*seg1.length
            or bend.dot(Vector(POLE_FALLBACK[kind])) < 0):
        bend = Vector(POLE_FALLBACK[kind])
        bend -= axis.normalized()*(bend.dot(axis.normalized()))
        if bend.length < 1e-9:
            return
    # Hinge axis: flexion rotates seg1 toward the bend side, so the
    # (right-handed) hinge is bend_dir x seg1 -- not axis x bend_dir,
    # which points the opposite way and mirrors the flexion side.
    normal = bend.normalized().cross(seg1.normalized())
    if normal.length < 1e-9:
        return
    normal.normalize()

    def e(r, row, col):
        return r.col[col][row]

    for i in (0, 1):
        pb = ctrl.pose.bones[f'MCH_{limb}_{i}']
        rest = pb.bone.matrix_local.to_3x3()
        dots = [abs(rest.col[k].dot(normal)) for k in range(3)]
        hinge = dots.index(max(dots))
        # Both segment directions rotate about the bend-plane normal to
        # close the joint (the normal is defined by root->mid x mid->end),
        # so flexion has the same armature-space sense for both bones.
        direction = normal
        s_local = 1.0 if rest.col[hinge].dot(direction) >= 0 else -1.0
        r = pb.matrix_basis.to_3x3()
        tau = 2*math.atan2(e(r, 0, 2)-e(r, 2, 0),
                           e(r, 0, 0)+e(r, 1, 1)+e(r, 2, 2)+1)
        tau = (tau+math.pi) % (2*math.pi)-math.pi
        num = 2*(1+e(r, 1, 1))
        if num < 1e-4:
            continue  # segment flipped ~180 deg: solver limits unreliable
        scale = 1/math.sqrt(num)
        swing = {0: -e(r, 2, 1)*scale, 2: e(r, 0, 1)*scale}
        # Flexion side of each component, measured from the actual bent
        # creation pose when possible: computing it from bind frames
        # flips sign on mirrored/rotated chains.  Both swing components
        # get the full flexion radius on their flexion side (the solver
        # ellipse-clamps the 2D swing vector, so a diagonal bend must not
        # be choked by the side semi-axis); the hinge keeps a tight
        # hyperextension side.  On spine/neck bones the hinge can be the
        # local Y (twist) parameter itself; then flexion is limited
        # through the twist interval (tau is the local Y angle).
        params = {0: swing[0], 1: tau, 2: swing[2]}
        hc = params[hinge]
        if hinge == 1:
            hinge_measured = abs(hc) > 0.05
            flex_sign = (1.0 if hc > 0 else -1.0) if hinge_measured else s_local
        else:
            hinge_measured = abs(hc) > 0.03
            flex_sign = (1.0 if hc > 0 else -1.0) if hinge_measured else -s_local
        ranges = {}
        for k in (0, 1, 2):
            center = params[k]
            if k == hinge:
                sign, opp_ang, flex_ang = flex_sign, HYPER_EXTENSION, profile['flex'][i]
            elif k == 1:
                sign, opp_ang = 0.0, profile['twist'][i]
                flex_ang = profile['twist'][i]
            else:
                opp_ang = profile['side'][i]
                flex_ang = profile['flex'][i]
                sign = (1.0 if center > 0 else -1.0) if abs(center) > 0.05 else 0.0

            def param(angle, k=k):
                return angle if k == 1 else math.sin(angle/2)
            if k == hinge and (i == 0 or not hinge_measured):
                # Root joints (hip/shoulder/spine base) are ball joints,
                # and on a straight/flipped creation pose the flexion
                # side of a mid joint cannot be measured reliably either:
                # guessing it pins the whole chain (a leg that cannot
                # fold).  Give those the flexion radius on both sides;
                # the pole still aims the bend, and the fold angle stays
                # bounded.  A clearly bent creation pose keeps the tight
                # hyperextension side.
                low, high = -param(flex_ang), param(flex_ang)
            elif sign > 0:
                low, high = -param(opp_ang), param(flex_ang)
            elif sign < 0:
                low, high = -param(flex_ang), param(opp_ang)
            else:
                low, high = -param(opp_ang), param(opp_ang)
            low, high = min(low, center), max(high, center)
            # Small safety margin: solver parameters drift slightly
            # between the calibration pose and other valid poses.
            margin = 0.10 if k == 1 else 0.02
            low, high = low-margin, high+margin
            if k == 1:
                low, high = max(-math.pi, low), min(math.pi, high)
            ranges[k] = (low, high)
        for k, axis_name in enumerate('xyz'):
            if k == 1 and ((i == 1 and hinge != 1) or abs(tau) > 2.2):
                # No twist limit for end segments (their twist parameter
                # sits near its pi singularity and swings wildly between
                # equivalent poses), nor for any bone whose calibrated
                # twist is near the +/-pi wrap: the resting value would
                # sit on the interval edge and the clamp rolls the whole
                # chain.  Hinge swing limits still apply.  Remove a
                # stale driver from an older calibration, or it would
                # simply switch the limit back on.
                setattr(pb, f'use_ik_limit_{axis_name}', False)
                stale = f'pose.bones["{pb.name}"].use_ik_limit_{axis_name}'
                adata = ctrl.animation_data
                if adata:
                    for fcurve in list(adata.drivers):
                        if fcurve.data_path == stale:
                            adata.drivers.remove(fcurve)
                continue
            low, high = ranges[k]
            if k == 1:
                ik_min, ik_max = low, high
            else:
                # Invert the solver's -sin(angle/2) parameterisation.
                ik_min = -2*math.asin(max(-1.0, min(1.0, high)))
                ik_max = -2*math.asin(max(-1.0, min(1.0, low)))
            setattr(pb, f'ik_min_{axis_name}', ik_min)
            setattr(pb, f'ik_max_{axis_name}', ik_max)
            setattr(pb, f'use_ik_limit_{axis_name}', True)
            # Recalibration (Match on an older rig) must not stack
            # duplicate drivers on the same property.
            path = f'pose.bones["{pb.name}"].use_ik_limit_{axis_name}'
            adata = ctrl.animation_data
            if adata:
                for fcurve in list(adata.drivers):
                    if fcurve.data_path == path:
                        adata.drivers.remove(fcurve)
            driven = pb.driver_add(f'use_ik_limit_{axis_name}').driver
            driven.type = 'SCRIPTED'; driven.expression = 'value'
            var = driven.variables.new(); var.name = 'value'
            var.type = 'SINGLE_PROP'; var.targets[0].id = ctrl
            var.targets[0].data_path = '["bd_limits"]'

def create(source, ik_default=False):
    if source is None or source.type != 'ARMATURE' or not source.get('gmd_source'):
        raise ValueError('Select an imported GMD armature')
    if source.get('bd_control_rig'):
        raise ValueError('A control rig already exists; select it or remove it first')
    missing = [n for names in LIMBS.values() for n in names if n not in source.data.bones]
    missing += [n for n in SHOULDERS.values() if n not in source.data.bones]
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
    prop(ctrl,'bd_helpers',1.0,description='Game-style muscle/twist helper bones (approximate sharing; not exact OPE0)')
    for limb in LIMBS: prop(ctrl,'bd_ik_'+limb,0.0,description='0: FK / 1: IK (use Match buttons to avoid jumps)')
    for key in SHOULDERS: prop(ctrl,'bd_'+key,0.0,description='0: FK clip / 1: shoulder (clavicle) control')
    prop(ctrl,'bd_shoulder_assist',0.35,description='Clavicle auto-follow: shoulder girdle rises/reaches with the arm in IK')
    prop(ctrl,'bd_limits',1.0,description='Respect anatomical joint limits in IK solving')
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
            a,b = (source.data.bones[n] for n in names[:2])
            c = source.data.bones[names[2]] if len(names) > 2 else None
            end_bone = c if c is not None else b
            joints = [a.head_local.copy(), b.head_local.copy()]
            joints.append(c.head_local.copy() if c is not None else
                          _neck_end(b.matrix_local, b))
            rootparent = a.parent.name if a.parent else 'CTRL_body'
            for i,first in enumerate((a,b)):
                mechanism = _new_bone(data,f'MCH_{limb}_{i}',joints[i],joints[i+1],
                          rootparent if i==0 else f'MCH_{limb}_0',first.matrix_local)
                # Anatomical mechanism frame: local X is the hinge axis
                # (armature X, the character's mediolateral axis), so the
                # IK solver's swing/twist limit parameters stay meaningful
                # instead of following arbitrary GMD bone rolls.
                seg = joints[i+1]-joints[i]
                if seg.length > 1e-9:
                    seg.normalize()
                    hinge_x = Vector((1,0,0))
                    hinge_x -= seg*hinge_x.dot(seg)
                    if hinge_x.length > 1e-6:
                        mechanism.align_roll(hinge_x.normalized().cross(seg))
                if i == 1:
                    # Blender's IK solver expects a genuinely connected
                    # two-bone chain; the GMD clone itself stays disconnected.
                    mechanism.use_connect = True
                _new_bone(data,f'OUT_{limb}_{i}',first.head_local,first.tail_local,
                          f'MCH_{limb}_{i}',first.matrix_local)
            # A location-only end frame on the solved lower segment prevents
            # COPY_TRANSFORMS-to-goal from stretching an unreachable limb.
            if KINDS[limb] == 'arm':
                # Wrist frame rigidly attached to the forearm segment:
                # in IK the hand follows the forearm (plus the hand
                # control's own rotation, applied as an offset), instead
                # of keeping the control's world orientation and bending
                # passively at the wrist.
                _new_bone(data,f'MCH_hand_{limb}',joints[2],
                          joints[2]+(end_bone.tail_local-end_bone.head_local),
                          f'MCH_{limb}_1',end_bone.matrix_local)
            end_frame = _new_bone(data,f'OUT_{limb}_2',joints[2],
                      joints[2]+(end_bone.tail_local-end_bone.head_local),
                      f'MCH_{limb}_1',end_bone.matrix_local)
            end_frame.use_connect = True
            name='CTRL_'+limb
            _new_bone(data,name,joints[2],joints[2]+Vector((0,size*.12,0)),'CTRL_global')
            goalparent=name
            if limb.startswith('leg'):
                toe = source.data.bones.get('asi4_'+limb[-1]+'_n')
                heel=Vector((joints[2].x,0,joints[2].z-size*.08))
                tip=toe.head_local.copy() if toe else joints[2]+Vector((0,0,size*.14))
                tip.y=0
                for pivot,point in (('heel',heel),('toe',tip)):
                    pname='MCH_'+limb+'_'+pivot
                    _new_bone(data,pname,point,point+Vector((0,size*.08,0)),goalparent)
                    goalparent=pname
            _new_bone(data,'MCH_goal_'+limb,joints[2],
                      joints[2]+(end_bone.tail_local-end_bone.head_local),
                      goalparent,end_bone.matrix_local)
            pole=_pole(joints[0],joints[1],joints[2],size*.5,
                       POLE_FALLBACK[KINDS[limb]])
            _new_bone(data,'CTRL_pole_'+limb,pole,pole+Vector((0,size*.1,0)),'CTRL_global')
        for key,bone_name in SHOULDERS.items():
            clavicle = source.data.bones[bone_name]
            _new_bone(data,'CTRL_'+key,clavicle.head_local,
                      clavicle.head_local+Vector((0,size*.1,0)),'CTRL_global')
            # The mechanism rides the chest (mune) so the clavicle stays
            # attached when the spine bends; auto-follow is driven onto
            # the base bone, the manual control rotates the inner bone.
            _new_bone(data,'MCH_'+key+'_base',clavicle.head_local,
                      clavicle.tail_local,'mune_c_n',clavicle.matrix_local)
            _new_bone(data,'MCH_'+key,clavicle.head_local,clavicle.tail_local,
                      'MCH_'+key+'_base',clavicle.matrix_local)
        # Driver bind frames for optional helper sharing; no evaluated back-reference.
        from . import mmo
        from pathlib import Path
        rig=mmo.parse_moma((Path(__file__).parent/'rigs/p_man.mma').read_bytes())
        helper_drivers={}
        for op in rig['operations']:
            name=rig['nodes'][op['target']]
            if name in HELPER_SHARE_EXCLUDED: continue
            drivers=[rig['nodes'][i] for i in op['drivers'] if i>=0]
            if name not in source.data.bones or len(drivers)<2: continue
            original=source.data.bones[name]
            # Share between the two driver bones nearest the helper:
            # muscle/twist helpers sit between the joints they serve.
            present=[d for d in drivers if d in source.data.bones]
            present.sort(key=lambda d: (source.data.bones[d].head_local
                                        - original.head_local).length)
            if len(present)<2: continue
            # The helper rides its natural parent (one of the drivers);
            # it additionally takes a share of the other nearest
            # driver's local rotation, distributing the joint's
            # twist/bend over the helper instead of letting the skin
            # collapse at the joint.
            parent_name = original.parent.name if original.parent else None
            others = [d for d in present[:2] if d != parent_name]
            helper_drivers[name] = others[0] if others else present[0]
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
            if KINDS[limb] == 'arm':
                wrist = ctrl.pose.bones[f'MCH_hand_{limb}']
                offset = wrist.constraints.new('COPY_ROTATION')
                offset.name = PREFIX+'wrist offset '+limb
                offset.target = ctrl; offset.subtarget = 'CTRL_'+limb
                offset.target_space = 'LOCAL'; offset.owner_space = 'LOCAL'
                offset.mix_mode = 'AFTER'
            ik.name=PREFIX+'IK '+limb; ik.target=ctrl; ik.subtarget='MCH_goal_'+limb
            ik.pole_target=ctrl; ik.pole_subtarget='CTRL_pole_'+limb
            ik.chain_count=2; ik.use_tail=True; ik.use_stretch=False
            ik.pole_angle=_pole_angle(ctrl,limb)
            for i in (0,1):
                ctrl.pose.bones[f'MCH_{limb}_{i}'].ik_stretch=0
                if KINDS[limb] == 'neck' and i == 1:
                    continue  # face_c_n is driven as the neck endpoint below
                _copy(ctrl.pose.bones[names[i]],ctrl,f'OUT_{limb}_{i}',ctrl,'bd_ik_'+limb)
            if KINDS[limb] == 'neck':
                endpoint = ctrl.pose.bones[names[1]]
                end_subtarget = f'OUT_{limb}_1'
            else:
                endpoint = ctrl.pose.bones[names[2]]
                end_subtarget = f'OUT_{limb}_2'
            location = endpoint.constraints.new('COPY_LOCATION')
            location.name = PREFIX+'IK position '+limb
            location.target = ctrl; location.subtarget = end_subtarget
            location.owner_space = location.target_space = 'WORLD'
            driver(location, ctrl, 'bd_ik_'+limb)
            _copy(endpoint, ctrl,
                  f'MCH_hand_{limb}' if KINDS[limb] == 'arm'
                  else 'MCH_goal_'+limb,
                  ctrl, 'bd_ik_'+limb, rotation_only=True)
            if limb.startswith('leg'):
                foot=ctrl.pose.bones['CTRL_'+limb]
                prop(foot,'roll',0.0,-1.3,1.3,'Foot roll in radians: negative heel / positive toe')
                for pivot,expr in (('heel','min(roll,0)'),('toe','max(roll,0)')):
                    pb=ctrl.pose.bones[f'MCH_{limb}_{pivot}']; pb.rotation_mode='XYZ'
                    d=pb.driver_add('rotation_euler',0).driver; d.type='SCRIPTED'; d.expression=expr
                    v=d.variables.new(); v.name='roll'; v.type='SINGLE_PROP'; v.targets[0].id=ctrl
                    v.targets[0].data_path=foot.path_from_id()+'["roll"]'
        for key,bone_name in SHOULDERS.items():
            arm_key = 'arm_'+key[-1]
            follow = _copy(ctrl.pose.bones[bone_name], ctrl, 'MCH_'+key,
                           ctrl, 'bd_'+key)
            # The clavicle follows its mechanism whenever the animator
            # enables it manually OR the arm is in IK (auto-follow).
            adata = ctrl.animation_data
            fpath = (f'pose.bones["{bone_name}"].constraints'
                     f'["{follow.name}"].influence')
            for fcurve in list(adata.drivers):
                if fcurve.data_path == fpath:
                    adata.drivers.remove(fcurve)
            gate = follow.driver_add('influence').driver
            gate.type = 'SCRIPTED'
            gate.expression = 'max(manual, armik)'
            var = gate.variables.new(); var.name = 'manual'
            var.type = 'SINGLE_PROP'; var.targets[0].id = ctrl
            var.targets[0].data_path = f'["bd_{key}"]'
            var = gate.variables.new(); var.name = 'armik'
            var.type = 'SINGLE_PROP'; var.targets[0].id = ctrl
            var.targets[0].data_path = f'["bd_ik_{arm_key}"]'
            # Manual clavicle offset: rotating CTRL_<key> (its basis
            # rotation is zeroed by Match) rotates the mechanism bone.
            mech = ctrl.pose.bones['MCH_'+key]
            manual = mech.constraints.new('COPY_ROTATION')
            manual.name = PREFIX+'clavicle manual '+key
            manual.target = ctrl; manual.subtarget = 'CTRL_'+key
            manual.target_space = 'LOCAL'; manual.owner_space = 'LOCAL'
            manual.mix_mode = 'AFTER'; manual.influence = 1.0
            # Auto-follow drivers on the base bone: clavicle elevation
            # (local Z) from hand height and protraction (local Y) from
            # forward reach, scaled by bd_shoulder_assist in arm IK.
            base = ctrl.pose.bones['MCH_'+key+'_base']
            base.rotation_mode = 'XYZ'
            clavicle = source.data.bones[bone_name]
            sign = 1.0 if key.endswith('_l') else -1.0
            reach = 0.5*size
            # World forward offset of the bind wrist vs the clavicle:
            # protraction is measured from the bind pose, not from zero.
            bind_forward = (source.data.bones[LIMBS[arm_key][2]].head_local.z
                            - clavicle.head_local.z)
            # Elevation/protraction from the hand's world position
            # relative to the shoulder control (also world): zero while
            # the hand hangs below the shoulder, as in FK clips, so
            # matching and clip conversion stay pop-free.
            for axis_index, expr, htype, stype in (
                    (2, f'{sign} * armik * assist * 1.2 * min(1.0, max(0.0, '
                        f'(hand - sho) / {reach:.4f}))', 'LOC_Z', 'LOC_Z'),
                    (1, f'{-sign} * armik * assist * 0.6 * min(1.0, max(0.0, '
                        f'((sho - hand) - {bind_forward:.4f}) / {reach:.4f}))',
                     'LOC_Y', 'LOC_Y')):
                driven = base.driver_add('rotation_euler', axis_index).driver
                driven.type = 'SCRIPTED'; driven.expression = expr
                var = driven.variables.new(); var.name = 'hand'
                var.type = 'TRANSFORMS'; var.targets[0].id = ctrl
                var.targets[0].bone_target = 'CTRL_'+arm_key
                var.targets[0].transform_type = htype
                var.targets[0].transform_space = 'WORLD_SPACE'
                var = driven.variables.new(); var.name = 'sho'
                var.type = 'TRANSFORMS'; var.targets[0].id = ctrl
                var.targets[0].bone_target = 'CTRL_'+key
                var.targets[0].transform_type = stype
                var.targets[0].transform_space = 'WORLD_SPACE'
                var = driven.variables.new(); var.name = 'armik'
                var.type = 'SINGLE_PROP'; var.targets[0].id = ctrl
                var.targets[0].data_path = f'["bd_ik_{arm_key}"]'
                var = driven.variables.new(); var.name = 'assist'
                var.type = 'SINGLE_PROP'; var.targets[0].id = ctrl
                var.targets[0].data_path = '["bd_shoulder_assist"]'
        for name, other in helper_drivers.items():
            share = ctrl.pose.bones[name].constraints.new('COPY_ROTATION')
            share.name = PREFIX+'helper share '+name
            share.target = ctrl; share.subtarget = other
            share.target_space = 'LOCAL'; share.owner_space = 'LOCAL'
            driver(share, ctrl, 'bd_helpers', 0.35)
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


def _calibrate_pole(ctrl, limb, desired, endpoint, tip_frame=None,
                    use_frame=False):
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
        if use_frame and tip_frame is not None:
            # Neck: the mechanism mid joint is not displayed (the face
            # clone takes position from the chain tip and rotation from
            # the goal), while a rolled resting frame flips the chain
            # when the goal moves.  Calibrate the solved tip frame onto
            # the frame the mechanism bases were built from.
            x = ctrl.pose.bones['MCH_'+limb+'_1'].matrix.to_3x3().col[0]
            v = x-axis*x.dot(axis)
            xt = tip_frame.to_3x3().col[0]
            wt = xt-axis*xt.dot(axis)
            if v.length > 1e-6 and wt.length > 1e-6:
                return math.atan2(axis.dot(v.cross(wt)), v.dot(wt))
        joint = ctrl.pose.bones['MCH_'+limb+'_1'].head.copy()
        v = joint-root
        v -= axis*v.dot(axis)
        if v.length < 1e-9:
            return None
        return math.atan2(axis.dot(v.cross(wanted)), v.dot(wanted))

    def set_angle(value):
        # pole_angle is only defined modulo 2pi; keep it inside [-pi, pi]
        # so Newton steps can cross the wrap instead of clamping there.
        ik.pole_angle = (value+math.pi) % (2*math.pi)-math.pi

    error = signed_error()
    if error is None or abs(error) < 1e-6:
        return
    start_angle = ik.pole_angle
    set_angle(start_angle+0.1)
    probed = signed_error()
    if probed is None:
        set_angle(start_angle)
        return
    sensitivity = (probed-error)/0.1
    if abs(sensitivity) < 1e-6:
        set_angle(start_angle)
        refresh(ctrl)
        return
    set_angle(ik.pole_angle-probed/sensitivity)
    for _ in range(6):
        error = signed_error()
        if error is None or abs(error) < 1e-6:
            break
        set_angle(ik.pole_angle-error/sensitivity)
    error = signed_error()
    if error is not None and abs(error) > 0.02:
        # Newton landed in a flipped basin (short/near-straight chains
        # have near-zero sensitivity until the plane snaps).  Scan the
        # full circle and keep the angle whose solved joint best
        # matches the FK bend plane.
        best = (abs(error), ik.pole_angle)
        for k in range(16):
            set_angle(-math.pi+k*(math.pi/8))
            scanned = signed_error()
            if scanned is not None and abs(scanned) < best[0]:
                best = (abs(scanned), ik.pole_angle)
        set_angle(best[1])
    refresh(ctrl)

def _segment_matrix(start, end, hinge):
    """World frame of one mechanism segment: Y along the segment, local X
    on the hinge (bend-plane normal) axis."""
    y = end-start
    if y.length < 1e-9:
        y = Vector((0,1,0))
    else:
        y = y.normalized()
    x = hinge.copy()
    x -= y*x.dot(y)
    if x.length < 1e-6:
        x = Vector((1,0,0))
        x -= y*x.dot(y)
    x.normalize()
    z = x.cross(y)
    result = Matrix(((x[0], y[0], z[0]), (x[1], y[1], z[1]), (x[2], y[2], z[2]))).to_4x4()
    result.translation = start
    return result


def _set_bone_matrix(pb, matrix):
    """Set a pose bone's armature-space matrix via its basis directly.

    The PoseBone.matrix setter decomposes against the *evaluated* parent
    frame; for connected mechanism bones under a live IK solver that
    decomposition can roll the basis 180 degrees about the bone axis
    (positions stay right, the basis flips), which poisons the solver's
    swing/twist limit decomposition.  Converting through the rest frame
    keeps the basis clean.
    """
    bone = pb.bone
    if pb.parent is not None:
        pb.matrix_basis = bone.convert_local_to_pose(
            matrix, bone.matrix_local, parent_matrix=pb.parent.matrix,
            parent_matrix_local=pb.parent.bone.matrix_local, invert=True)
    else:
        pb.matrix_basis = bone.convert_local_to_pose(
            matrix, bone.matrix_local, invert=True)


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
    pole_matrix.translation = _pole(*(m.translation for m in matrices),
                                    size*.5, POLE_FALLBACK[KINDS[limb]])
    pole.matrix = pole_matrix
    refresh(ctrl)
    # GMD bind chains are perfectly straight, which is a singularity for
    # Blender's pole-target IK: it then refuses to bend.  Pose the mechanism
    # segments onto the FK solution first, breaking the singularity on the
    # correct (pole) side; the live solver starts from this basis.
    joints = [m.translation for m in matrices]
    # A perfectly straight FK chain (a rest-pose leg) leaves pole-target
    # IK at its singularity: the solver then ignores closer goals or
    # flips the chain through the root.  Bend the mechanism's starting
    # bases slightly toward the pole.  With the goal on the FK endpoint
    # the solver still settles on the exact FK pose, but it keeps a bent
    # initial guess, so later goal moves bend on the pole side.
    seg1 = joints[1]-joints[0]
    seg2 = joints[2]-joints[1]
    if (seg1.length > 1e-9 and seg2.length > 1e-9
            and seg1.angle(seg2) < 0.03):
        pole_dir = pole.matrix.translation-joints[1]
        axis = joints[2]-joints[0]
        pole_dir -= axis*(pole_dir.dot(axis)/axis.length_squared)
        if pole_dir.length < 1e-6:
            pole_dir = Vector(POLE_FALLBACK[KINDS[limb]])
            pole_dir -= axis.normalized()*pole_dir.dot(axis.normalized())
        if pole_dir.length > 1e-9:
            joints[1] += pole_dir.normalized() * \
                (0.008*min(seg1.length, seg2.length))
    hinge = (joints[1]-joints[0]).cross(joints[2]-joints[1])
    if hinge.length < 0.12*seg1.length*seg2.length:
        # Near-straight: hinge from the (anatomical) pole instead of the
        # noise-dominated joint cross product.
        hinge = (pole.matrix.translation-joints[1]).cross(
            joints[2]-joints[0])
    if hinge.length < 1e-6:
        hinge = Vector((1,0,0))
    hinge.normalize()
    # Set the segment bases with the solver muted: converted against a
    # live solver's transient frame, a basis silently encodes that
    # transient (the solver was already compensating it), and the chain
    # drifts as soon as an ancestor chain changes state.  Muted, the
    # evaluated frame is the basis itself, so the conversion anchors
    # the mechanism canonically; the solver then starts from these
    # bases at every later evaluation.
    ik_constraint = next(
        (c for c in ctrl.pose.bones[f'MCH_{limb}_1'].constraints
         if c.type == 'IK'), None)
    was_muted = ik_constraint.mute if ik_constraint is not None else False
    if ik_constraint is not None:
        ik_constraint.mute = True
    try:
        for i in (0, 1):
            mechanism = ctrl.pose.bones[f'MCH_{limb}_{i}']
            refresh(ctrl)
            _set_bone_matrix(mechanism, _segment_matrix(
                joints[i], joints[i+1], hinge))
            refresh(ctrl)
    finally:
        if ik_constraint is not None:
            ik_constraint.mute = was_muted
    _calibrate_pole(ctrl, limb, matrices[1].translation,
                    matrices[2].translation,
                    tip_frame=_segment_matrix(joints[1], joints[2], hinge),
                    use_frame=(KINDS[limb] == 'neck'))
    # Pole direction fixes the two joint positions, but not necessarily the
    # axial frame of either segment.  The OUT bones absorb that difference.
    for i in (0, 1):
        out = ctrl.pose.bones[f'OUT_{limb}_{i}']
        # Keep the output frame exactly on the solved mechanism joint.  Only
        # its axial orientation is compensated; absorbing a positional error
        # here would let the displayed segment lengths drift during motion.
        location, rotation, scale = matrices[i].decompose()
        refresh(ctrl)
        _set_bone_matrix(out, Matrix.LocRotScale(out.head.copy(),
                                                 rotation, scale))
        refresh(ctrl)
        if key:
            out.rotation_mode = 'QUATERNION'
            for path in ('location', 'rotation_quaternion', 'scale'):
                out.keyframe_insert(data_path=path)
    if KINDS[limb] == 'arm':
        # Pin the wrist frame to the FK wrist at this pose (it then
        # follows the solved forearm rigidly), and make the control's
        # own rotation the zero reference for the wrist offset, so a
        # plain translation of the hand control never bends the wrist.
        hand = ctrl.pose.bones[f'MCH_hand_{limb}']
        refresh(ctrl)
        _set_bone_matrix(hand, matrices[2])
        loc, _rot, scale = control.matrix_basis.decompose()
        control.matrix_basis = Matrix.LocRotScale(loc, Quaternion(), scale)
        refresh(ctrl)
        if key:
            hand.rotation_mode = 'QUATERNION'
            for path in ('location', 'rotation_quaternion', 'scale'):
                hand.keyframe_insert(data_path=path)
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


# Chains whose mechanism roots ride another chain's output: the arm and
# neck roots sit on the spine, so healing the spine moves their parent
# frame under already-anchored mechanism bases.
DEPENDENTS = {'spine': ('arm_l', 'arm_r', 'neck')}


def _settle(ctrl, limb, matrices, key):
    """Re-pose a limb from its FK reference until the evaluated chain
    sits on it (Blender's IK is history-dependent, so the frame the OUT
    bones were compensated against can differ from the settled one)."""
    names = LIMBS[limb]
    for _ in range(5):
        refresh(ctrl)
        diff = max(abs(ctrl.pose.bones[n].matrix[i][j]-m[i][j])
                   for n, m in zip(names, matrices)
                   for i in range(4) for j in range(4))
        if diff <= 2e-4:
            break
        _position_ik_controls(ctrl, limb, matrices, key=key)
        _apply_limits(ctrl, limb, [m.translation for m in matrices])


def match(ctrl,limb,direction,key=True,enable=True):
    names=LIMBS[limb]; refresh(ctrl)
    if key:
        _key_previous_switch(ctrl, limb, direction)
    if direction=='IK':
        # Read the FK reference with every chain's IK blend off.  With
        # blends on, the clones show current IK results -- and a limb's
        # parents ride other chains (shoulders sit on the spine), so
        # matching one limb against another's twisted output poisons
        # its goals and preserves the twist forever.  The FK bases are
        # the ground truth a repair must return to.
        saved={name: ctrl['bd_ik_'+name] for name in LIMBS}
        for name in LIMBS:
            ctrl['bd_ik_'+name]=0.0
        refresh(ctrl)
        matrices=_joint_matrices(ctrl, limb)
        _position_ik_controls(ctrl, limb, matrices, key=key)
        for name,value in saved.items():
            ctrl['bd_ik_'+name]=value
        # Rigs saved by older versions keep their limit values as data;
        # recalibrating here repairs them without recreating the rig.
        _apply_limits(ctrl, limb, [m.translation for m in matrices])
        ctrl['bd_ik_'+limb]=1.0 if enable else 0.0
        if enable and KINDS[limb] == 'arm':
            # The clavicle follows its mechanism whenever the arm is in
            # IK; match it onto the FK pose before the final settle, or
            # the switch would replace the clip's clavicle motion with
            # the bind frame.  (Settling afterwards keeps the arm chain
            # on the FK reference in the true final state.)
            match_shoulder(ctrl, 'shoulder_'+limb[-1], key_switch=key)
        if enable:
            _settle(ctrl, limb, matrices, key)
            # Healing this chain moved the parent frame of its dependent
            # chains (arms/neck ride the spine).  Re-settle a dependent
            # whose goal still sits on its FK endpoint -- a hand-posed
            # goal means an animator put it there; leave it alone.
            for dep in DEPENDENTS.get(limb, ()):
                saved_dep = {name: ctrl['bd_ik_'+name] for name in LIMBS}
                for name in LIMBS:
                    ctrl['bd_ik_'+name] = 0.0
                refresh(ctrl)
                dep_matrices = _joint_matrices(ctrl, dep)
                for name, value in saved_dep.items():
                    ctrl['bd_ik_'+name] = value
                refresh(ctrl)
                goal = ctrl.pose.bones['MCH_goal_'+dep].matrix
                if KINDS[dep] == 'arm':
                    # Arm goals carry no orientation reference any more
                    # (the wrist follows the forearm): "unposed" means
                    # the control was neither moved nor rotated.
                    _loc, dep_quat, _scale = \
                        ctrl.pose.bones['CTRL_'+dep].matrix_basis.decompose()
                    unposed = (
                        (goal.translation-dep_matrices[2].translation).length
                        <= 2e-3
                        and dep_quat.rotation_difference(Quaternion()).angle
                        <= 0.05)
                else:
                    unposed = max(abs(goal[i][j]-dep_matrices[2][i][j])
                                  for i in range(4)
                                  for j in range(4)) <= 2e-3
                if unposed:
                    _settle(ctrl, dep, dep_matrices, key)
    else:
        matrices=[ctrl.pose.bones[n].matrix.copy() for n in names]
        ctrl['bd_ik_'+limb]=0.0; refresh(ctrl)
        for name,mat in zip(names,matrices):
            pb=ctrl.pose.bones[name]; pb.matrix=mat; refresh(ctrl)
            if key:
                pb.rotation_mode='QUATERNION'
                for path in ('location','rotation_quaternion','scale'): pb.keyframe_insert(data_path=path)
    if key: ctrl.keyframe_insert(data_path='["bd_ik_'+limb+'"]')
    refresh(ctrl)


def match_shoulder(ctrl, key, key_switch=True):
    """Place a clavicle control on the current kata pose and enable it."""
    # Same FK-reference rule as match(): kata rides the spine output,
    # so read it with every IK blend off.
    saved={name: ctrl['bd_ik_'+name] for name in LIMBS}
    saved_sh={k: ctrl['bd_'+k] for k in SHOULDERS}
    for name in LIMBS:
        ctrl['bd_ik_'+name]=0.0
    for k in SHOULDERS:
        ctrl['bd_'+k]=0.0
    refresh(ctrl)
    target = ctrl.pose.bones[SHOULDERS[key]].matrix.copy()
    control = ctrl.pose.bones['CTRL_'+key]
    if key_switch:
        scene = bpy.context.scene
        action = ctrl.animation_data.action if ctrl.animation_data else None
        start = int(action.frame_range[0]) if action else scene.frame_current
        if scene.frame_current > start:
            ctrl['bd_'+key] = 0.0
            ctrl.keyframe_insert(data_path=f'["bd_{key}"]',
                                 frame=scene.frame_current-1)
    # Place the widget on the clavicle; the manual offset stored in its
    # basis rotation is computed afterwards, against the live base bone
    # (auto-follow included), so enabling the shoulder never pops.
    control.matrix = Matrix.LocRotScale(target.translation.copy(),
                                        target.to_quaternion(),
                                        Vector((1, 1, 1)))
    for name, value in saved.items():
        ctrl['bd_ik_'+name]=value
    for k, value in saved_sh.items():
        ctrl['bd_'+k]=value
    ctrl['bd_'+key] = 1.0
    refresh(ctrl)
    base_quat = ctrl.pose.bones['MCH_'+key+'_base'].matrix.to_quaternion()
    delta = base_quat.inverted() @ target.to_quaternion()
    loc, _rot, scale = control.matrix_basis.decompose()
    control.matrix_basis = Matrix.LocRotScale(loc, delta, scale)
    refresh(ctrl)
    # The offset is applied by a constraint in a slightly different
    # frame than it was authored in; close the remaining error with
    # measured corrections on the clavicle itself, keeping the best.
    target_quat = target.to_quaternion()
    best = None
    for _ in range(4):
        current = ctrl.pose.bones[SHOULDERS[key]].matrix.to_quaternion()
        error = current.rotation_difference(target_quat).angle
        loc, quat, scale = control.matrix_basis.decompose()
        if best is not None and error > best[0]:
            control.matrix_basis = Matrix.LocRotScale(loc, best[1], scale)
            refresh(ctrl)
            break
        best = (error, quat.copy())
        if error < 1e-5:
            break
        fix = current.inverted() @ target_quat
        control.matrix_basis = Matrix.LocRotScale(loc, quat @ fix, scale)
        refresh(ctrl)
    if key_switch:
        control.rotation_mode = 'QUATERNION'
        for path in ('location', 'rotation_quaternion', 'scale'):
            control.keyframe_insert(data_path=path)
        ctrl.keyframe_insert(data_path=f'["bd_{key}"]')
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
    for key in SHOULDERS:
        ctrl['bd_'+key] = 0.0
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
    # Conversion must reproduce the FK clip exactly: the clavicle
    # auto-follow would add its own rotation on top of the clip's.
    old_assist = ctrl['bd_shoulder_assist'] if 'bd_shoulder_assist' in ctrl else None
    if old_assist is not None:
        ctrl['bd_shoulder_assist'] = 0.0
    old_helpers = ctrl['bd_helpers'] if 'bd_helpers' in ctrl else None
    if old_helpers is not None:
        ctrl['bd_helpers'] = 0.0
    try:
        for limb in limbs:
            ctrl['bd_ik_'+limb] = 0.0
        refresh(ctrl)
        # Key the clavicle controls onto the FK kata pose per frame:
        # in IK the clavicle follows its mechanism, so without keys the
        # clip's clavicle motion would be replaced by the bind frame.
        def key_shoulder_pass():
            for key in SHOULDERS:
                for frame in range(start, end+1):
                    scene.frame_set(frame)
                    match_shoulder(ctrl, key, key_switch=False)
                    ctl = ctrl.pose.bones['CTRL_'+key]
                    ctl.rotation_mode = 'QUATERNION'
                    for path in ('location', 'rotation_quaternion', 'scale'):
                        ctl.keyframe_insert(data_path=path)
                ctrl.keyframe_insert(data_path=f'["bd_{key}"]', frame=start)
                if end > start:
                    ctrl.keyframe_insert(data_path=f'["bd_{key}"]', frame=end)
        key_shoulder_pass()
        for limb in limbs:
            names = LIMBS[limb]
            scene.frame_set(start)
            _apply_limits(ctrl, limb,
                          [m.translation for m in _joint_matrices(ctrl, limb)])
            for frame in range(start, end+1):
                scene.frame_set(frame)
                matrices = _joint_matrices(ctrl, limb)
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
        if old_assist is not None:
            # Re-key the clavicles with the auto-follow back on: the
            # match delta is computed against the live (assisted) base,
            # so the converted clip stays exact with assist enabled.
            ctrl['bd_shoulder_assist'] = old_assist
            key_shoulder_pass()
            refresh(ctrl)
    finally:
        if old_assist is not None:
            ctrl['bd_shoulder_assist'] = old_assist
        if old_helpers is not None:
            ctrl['bd_helpers'] = old_helpers
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


class BDOMAIN_OT_match_shoulder(bpy.types.Operator):
    bl_idname='bdomain.match_shoulder'
    bl_label='Match shoulder (clavicle) control'
    bl_options={'REGISTER','UNDO'}
    shoulder: EnumProperty(items=[(n,n.replace('_',' ').title(),'') for n in SHOULDERS])
    def execute(self,context):
        source,ctrl=source_and_control(context.object)
        if not ctrl: return {'CANCELLED'}
        try: match_shoulder(ctrl,self.shoulder)
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
        self.report({'INFO'},'FK clip keyed onto IK controllers for all chains')
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
    box=layout.box();box.label(text='Animator controls (Blender IK) '+RIG_VERSION,icon='ARMATURE_DATA')
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
        row=box.row(align=True);row.prop(ctrl,'["bd_ik_'+limb+'"]',text=LABELS[limb],slider=True)
        for direction in ('IK','FK'):
            op=row.operator('bdomain.match_controls',text='Match '+direction)
            op.limb=limb;op.direction=direction
    for key in SHOULDERS:
        row=box.row(align=True)
        row.prop(ctrl,'["bd_'+key+'"]',text=key.replace('_',' ').title()+' (clavicle)',slider=True)
        op=row.operator('bdomain.match_shoulder',text='Match')
        op.shoulder=key
    box.prop(ctrl,'["bd_shoulder_assist"]',text='Shoulder assist (clavicle follows arm)',slider=True)
    box.prop(ctrl,'["bd_limits"]',text='Joint limits (IK only)',slider=True)
    box.label(text='CTRL_pole_* aim knees / elbows / spine / neck')
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


CLASSES=(BDOMAIN_OT_create_controls,BDOMAIN_OT_match_controls,BDOMAIN_OT_match_shoulder,
         BDOMAIN_OT_sync_controls,
         BDOMAIN_OT_convert_controls,BDOMAIN_OT_remove_controls,
         BDOMAIN_OT_bake_controls,BDOMAIN_OT_select_controls)
