# SPDX-License-Identifier: GPL-2.0-or-later
"""Optional Blender-side body adaptation, NOT an emulator of the game's IK.

Works in GMD/armature coordinates (Y up). No handlers or live constraints are
installed. Corrected matrices are baked into a separate Action by the addon.
"""
import math
from mathutils import Vector, Matrix


def two_bone(a, b, c, goal, fallback=None):
    """Reach a goal with fixed segment lengths, preserving the bend plane.

    Returns the new middle/end positions and whether reach was clamped. Bone
    display tails/lengths are deliberately unused: GMD joints are bone heads.
    """
    l1, l2 = (b-a).length, (c-b).length
    if min(l1, l2) < 1e-7:
        raise ValueError('zero-length IK segment')
    delta = goal-a
    axis = delta.normalized() if delta.length > 1e-8 else (c-a).normalized()
    if axis.length < .5:
        axis = Vector((0, -1, 0))
    minimum, maximum = abs(l1-l2)+1e-7, l1+l2-1e-7
    distance = max(minimum, min(maximum, delta.length))
    pole = b-a-axis*(b-a).dot(axis)
    if (pole.length < 1e-6 or (b-a).cross(c-b).length < 1e-6*l1*l2) and fallback is not None:
        pole = fallback-axis*fallback.dot(axis)
    if pole.length < 1e-6:
        reference = min((Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1))),
                        key=lambda v: abs(v.dot(axis)))
        pole = reference-axis*reference.dot(axis)
    pole.normalize()
    along = (l1*l1-l2*l2+distance*distance)/(2*distance)
    height = math.sqrt(max(0, l1*l1-along*along))
    return a+axis*along+pole*height, a+axis*distance, abs(distance-delta.length) > 1e-6


def propagate(worlds, parents, overrides):
    """Apply world-space overrides; descendants keep their old local SRT."""
    result = {}

    def visit(name):
        if name in result:
            return result[name]
        parent = parents[name]
        if name in overrides:
            result[name] = overrides[name].copy()
        elif parent:
            result[name] = visit(parent) @ worlds[parent].inverted_safe() @ worlds[name]
        else:
            result[name] = worlds[name].copy()
        return result[name]

    for name in worlds:
        visit(name)
    return result


def solve_chain(worlds, parents, names, goal, end_rotation=None, fallback=None):
    a, b, c = names
    pa, pb, pc = (worlds[n].translation.copy() for n in names)
    middle, end, clamped = two_bone(pa, pb, pc, goal, fallback)
    overrides = {}
    for name, old, new in ((a, pb-pa, middle-pa), (b, pc-pb, end-middle)):
        swing = old.rotation_difference(new).to_matrix()
        mat = (swing @ worlds[name].to_3x3()).to_4x4()
        mat.translation = pa if name == a else middle
        overrides[name] = mat
    mat = worlds[c].copy()
    if end_rotation is not None:
        mat = end_rotation.to_matrix().to_4x4()
    mat.translation = end
    overrides[c] = mat
    return propagate(worlds, parents, overrides), clamped


def parse_contacts(text, count):
    """Explicit inclusive Blender-frame ranges, e.g. '1-12, 30-42'."""
    if not text.strip():
        return None
    runs = []
    previous = 0
    for item in text.split(','):
        fields = item.strip().split('-')
        if len(fields) != 2 or not all(f.strip().isdigit() for f in fields):
            raise ValueError('Contacts must be inclusive ranges such as 1-12, 30-42')
        first, last = (int(f) for f in fields)
        if first < 1 or last < first or last > count or first <= previous:
            raise ValueError('Contact ranges must be ordered, non-overlapping and inside the Action')
        runs.append((first-1, last-1)); previous = last
    return runs


def detect_contacts(positions, fps, floor, ankle_height, height_tolerance, speed_limit, minimum=3):
    """Conservative flat-plane heuristic, not recovered game contact events.

    Both adjacent intervals must be slow to enter contact. This avoids locking
    the stationary apex of a jump unless it is also at the configured ground.
    """
    n = len(positions)
    speeds = [(positions[i+1]-positions[i]).length*fps for i in range(n-1)]
    mask = []
    for i, pos in enumerate(positions):
        speed = max(speeds[max(0, i-1):min(n-1, i+1)] or [0])
        height = pos.y-(floor+ankle_height)
        mask.append(abs(height) <= height_tolerance and speed <= speed_limit)
    runs = []; i = 0
    while i < n:
        if not mask[i]:
            i += 1; continue
        start = i
        while i+1 < n and mask[i+1]:
            i += 1
        if i-start+1 >= minimum:
            runs.append((start, i))
        i += 1
    return runs


def lock_targets(positions, rotations, runs, floor, ankle_height, blend_frames):
    """Return fixed stance goals plus eased entry/exit transitions.

    Horizontal anchor uses the interval midpoint, avoiding accumulated drift.
    The flat plateau is exactly fixed; blend frames do not count as locked.
    """
    targets = {}; plateaus = []
    for first, last in runs:
        edge = min(blend_frames, (last-first)//2)
        middle = (first+last)//2
        anchor = positions[middle].copy(); anchor.y = floor+ankle_height
        rot = rotations[middle]
        plateaus.append((first+edge, last-edge))
        for frame in range(first, last+1):
            distance = min(frame-first, last-frame)
            weight = 1 if edge == 0 else min(1, distance/edge)
            weight = weight*weight*(3-2*weight)
            targets[frame] = (positions[frame].lerp(anchor, weight),
                              rotations[frame].slerp(rot, weight))
    return targets, plateaus


def blend_helpers(worlds, parents, rest_worlds, operations, node_names):
    """Approximate twist/elbow/knee rotation sharing, NOT OPE0 execution.

    OPE0 provides target/driver identities. Only named twist/bend helpers are
    affected. Muscle/sterno/katapad operators remain untouched. Driver world
    deformation rotations are blended; helper bind SRT and offsets are kept.
    """
    overrides = {}; changed = []
    for op in operations:
        name = node_names[op['target']]
        if name not in worlds or not any(token in name for token in ('twist', 'elbow', 'knee')):
            continue
        drivers = [node_names[i] for i in op['drivers'] if i >= 0 and node_names[i] in worlds]
        if len(drivers) < 2:
            continue
        first, second = drivers[-2:]
        qa = (worlds[first].to_3x3() @ rest_worlds[first].to_3x3().inverted_safe()).to_quaternion()
        qb = (worlds[second].to_3x3() @ rest_worlds[second].to_3x3().inverted_safe()).to_quaternion()
        q = qa.slerp(qb, .5)
        mat = (q.to_matrix() @ rest_worlds[name].to_3x3()).to_4x4()
        mat.translation = worlds[name].translation
        overrides[name] = mat; changed.append(name)
    return propagate(worlds, parents, overrides), changed
