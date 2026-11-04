"""Minimal pure-Python stand-in for Blender's `mathutils` (Vector/Quaternion/Matrix).

Only the subset of the API used by the yk_gmd_io project's pure-Python layers
(gmdlib / meshlib / structurelib) is implemented.  Not a general-purpose
reimplementation - keep it small, keep it correct for the paths we take.

Vector: 2-4 components; x/y/z/w accessors, indexing, +, -, *, /, length,
         normalized, dot, cross, freeze, copy, xyz/xzy/zxy/yzx/zyx/yxz, resized
Quaternion: (w, x, y, z) constructor; w/x/y/z, conjugated, to_euler,
         to_matrix(3|4), @ (quaternion or vector rotation)
Matrix: 3x3 / 4x4 from rows; [i][j] + row/col accessors, transposed, inverted,
         copy, to_3x3, resize_4x4, decompose, to_quaternion,
         @ (Matrix/Vector), Identity/identity/Translation/Diagonal/LRotationTranslation
"""

import math as _m


class Vector:
    __slots__ = ("_v", "_frozen")

    def __init__(self, *args, **kwargs):
        # Accept Vector((x, y, z)), Vector(x, y, z), or Vector(x=, y=, z=)
        if kwargs:
            seq = (kwargs.get("x", 0), kwargs.get("y", 0), kwargs.get("z", 0))
        elif len(args) == 1 and not isinstance(args[0], (int, float, bool)):
            seq = args[0]
        else:
            seq = args
        seq = tuple(seq) if seq is not None else ()
        if len(seq) not in (2, 3, 4):
            raise TypeError(f"Vector needs 2-4 components, got {len(seq)}")
        self._v = tuple(float(x) for x in seq)
        self._frozen = False

    # -- component access --------------------------------------------------
    @property
    def x(self):
        return self._v[0]

    @x.setter
    def x(self, v):
        self._set(0, v)

    @property
    def y(self):
        return self._v[1]

    @y.setter
    def y(self, v):
        self._set(1, v)

    @property
    def z(self):
        if len(self._v) < 3:
            raise IndexError("vector has no z")
        return self._v[2]

    @z.setter
    def z(self, v):
        self._set(2, v)

    @property
    def w(self):
        if len(self._v) < 4:
            raise IndexError("vector has no w")
        return self._v[3]

    @w.setter
    def w(self, v):
        self._set(3, v)

    def _set(self, i, v):
        if self._frozen:
            raise TypeError("vector is frozen")
        self._v = self._v[:i] + (float(v),) + self._v[i + 1:]

    def __getitem__(self, i):
        return self._v[i]

    def __setitem__(self, i, v):
        self._set(i, v)

    def __len__(self):
        return len(self._v)

    def __iter__(self):
        return iter(self._v)

    # -- permutation properties (Blender API) ------------------------------
    @property
    def xyz(self):
        return Vector(self._v[0], self._v[1], self._v[2])

    @property
    def xzy(self):
        return Vector(self._v[0], self._v[2], self._v[1])

    @property
    def zxy(self):
        return Vector(self._v[2], self._v[0], self._v[1])

    @property
    def yzx(self):
        return Vector(self._v[1], self._v[2], self._v[0])

    @property
    def zyx(self):
        return Vector(self._v[2], self._v[1], self._v[0])

    @property
    def yxz(self):
        return Vector(self._v[1], self._v[0], self._v[2])

    def resized(self, n):
        v = list(self._v)
        while len(v) < n:
            v.append(0.0)
        return Vector(v[:n])

    # -- vector math ---------------------------------------------------------
    @property
    def length_squared(self):
        return sum(a * a for a in self._v)

    @property
    def length(self):
        return _m.sqrt(self.length_squared)

    def normalized(self):
        L = self.length
        if L == 0:
            raise ValueError("normalize: zero-length vector")
        return Vector(a / L for a in self._v)

    def dot(self, o):
        return sum(a * b for a, b in zip(self._v, o))

    def cross(self, o):
        a = self._v[:3]
        b = o[:3]
        return Vector((a[1] * b[2] - a[2] * b[1],
                       a[2] * b[0] - a[0] * b[2],
                       a[0] * b[1] - a[1] * b[0]))

    def copy(self):
        v = Vector(self._v)
        v._frozen = self._frozen
        return v

    def freeze(self):
        v = Vector(self._v)
        v._frozen = True
        return v

    def to_3d(self):
        return Vector(self._v[:3])

    # -- operators -----------------------------------------------------------
    def __add__(self, o):
        return Vector(a + b for a, b in zip(self._v, o))

    def __sub__(self, o):
        return Vector(a - b for a, b in zip(self._v, o))

    def __neg__(self):
        return Vector(-a for a in self._v)

    def __mul__(self, o):
        if isinstance(o, Vector):
            return Vector(a * b for a, b in zip(self._v, o))
        return Vector(a * o for a in self._v)

    def __rmul__(self, o):
        return self.__mul__(o)

    def __truediv__(self, o):
        if isinstance(o, Vector):
            return Vector(a / b for a, b in zip(self._v, o))
        return Vector(a / o for a in self._v)

    def __eq__(self, o):
        try:
            return len(self._v) == len(o) and all(a == b for a, b in zip(self._v, o))
        except TypeError:
            return NotImplemented

    def __hash__(self):
        if self._frozen:
            return hash(self._v)
        raise TypeError("unfrozen vectors are unhashable")

    def __repr__(self):
        return f"Vector({self._v!r})"


class Euler(Vector):
    """Euler angles - same storage as a 3-component Vector in this shim."""
    pass


class Quaternion:
    __slots__ = ("_w", "_x", "_y", "_z")

    def __init__(self, *args, **kwargs):
        # Blender: Quaternion((w, x, y, z)) or Quaternion(w, x, y, z) or ()
        if not args:
            self._w, self._x, self._y, self._z = 1.0, 0.0, 0.0, 0.0
            return
        if len(args) == 1 and not isinstance(args[0], (int, float, bool)):
            seq = tuple(args[0])
        else:
            seq = args
        if len(seq) != 4:
            raise TypeError(f"Quaternion needs 4 components, got {len(seq)}")
        self._w, self._x, self._y, self._z = (float(v) for v in seq)

    @property
    def w(self):
        return self._w

    @w.setter
    def w(self, v):
        self._w = float(v)

    @property
    def x(self):
        return self._x

    @x.setter
    def x(self, v):
        self._x = float(v)

    @property
    def y(self):
        return self._y

    @y.setter
    def y(self, v):
        self._y = float(v)

    @property
    def z(self):
        return self._z

    @z.setter
    def z(self, v):
        self._z = float(v)

    def __iter__(self):
        return iter((self._w, self._x, self._y, self._z))

    def __getitem__(self, i):
        return (self._w, self._x, self._y, self._z)[i]

    def conjugated(self):
        return Quaternion(self._w, -self._x, -self._y, -self._z)

    def copy(self):
        return Quaternion(self._w, self._x, self._y, self._z)

    def normalized(self):
        L = _m.sqrt(self._w ** 2 + self._x ** 2 + self._y ** 2 + self._z ** 2)
        if L == 0:
            return Quaternion(1, 0, 0, 0)
        return Quaternion(self._w / L, self._x / L, self._y / L, self._z / L)

    def to_euler(self, order="XYZ"):
        # XYZ convention (Blender default), returns Euler (x, y, z) radians
        w, x, y, z = self._w, self._x, self._y, self._z
        sinr_cosp = 2 * (w * x + y * z)
        cosr_cosp = 1 - 2 * (x * x + y * y)
        roll = _m.atan2(sinr_cosp, cosr_cosp)
        sinp = 2 * (w * y - z * x)
        if sinp >= 1:
            pitch = _m.pi / 2
        elif sinp <= -1:
            pitch = -_m.pi / 2
        else:
            pitch = _m.asin(sinp)
        siny_cosp = 2 * (w * z + x * y)
        cosy_cosp = 1 - 2 * (y * y + z * z)
        yaw = _m.atan2(siny_cosp, cosy_cosp)
        return Euler(roll, pitch, yaw)

    def to_matrix(self, size=4):
        x, y, z, w = self._x, self._y, self._z, self._w
        m = [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w), 0],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w), 0],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y), 0],
            [0, 0, 0, 1],
        ]
        if size == 3:
            m = [row[:3] for row in m[:3]]
        return Matrix(m)

    def __matmul__(self, o):
        if isinstance(o, Vector):
            # rotate the vector: v' = v + 2w(q x v) + 2(q x (q x v))
            q = Vector(self._x, self._y, self._z)
            v = o.xyz
            t = 2.0 * q.cross(v)
            return v + self._w * t + q.cross(t)
        w1, x1, y1, z1 = self._w, self._x, self._y, self._z
        w2, x2, y2, z2 = o._w, o._x, o._y, o._z
        return Quaternion(
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
        )

    def __eq__(self, o):
        if not isinstance(o, Quaternion):
            return NotImplemented
        return (self._w, self._x, self._y, self._z) == (o._w, o._x, o._y, o._z)

    def __repr__(self):
        return f"Quaternion({self._w}, {self._x}, {self._y}, {self._z})"


class Matrix:
    __slots__ = ("_r",)

    def __init__(self, rows=None):
        if rows is None:
            rows = 4
        if isinstance(rows, (int, float)):
            n = int(rows)
            rows = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
        self._r = [[float(v) for v in row] for row in rows]

    # -- constructors --------------------------------------------------------
    @classmethod
    def Identity(cls, n=4):
        return cls(n)

    identity = Identity

    @classmethod
    def Translation(cls, vector):
        n = len(vector)
        m = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
        for i in range(n):
            m[i][n - 1] = float(vector[i])
        return cls(m)

    @classmethod
    def Diagonal(cls, vector):
        n = len(vector)
        m = [[0.0] * n for _ in range(n)]
        for i in range(n):
            m[i][i] = float(vector[i])
        return cls(m)

    @classmethod
    def LRotationTranslation(cls, rotation, translation):
        m = rotation.to_matrix(4) if isinstance(rotation, Quaternion) else rotation
        t = cls.Identity(4)
        for i in range(4):
            for j in range(4):
                t._r[i][j] = m._r[i][j] if isinstance(m, Matrix) else m[i][j]
        for i, v in enumerate(translation[:3]):
            t._r[i][3] = float(v)
        return t

    # -- access --------------------------------------------------------------
    @property
    def row(self):
        return _RowAccessor(self)

    @property
    def col(self):
        return _ColAccessor(self)

    def __getitem__(self, i):
        return self._r[i]

    def __len__(self):
        return len(self._r)

    def __iter__(self):
        return iter(self._r)

    def size(self):
        return (len(self._r), len(self._r[0]))

    def copy(self):
        return Matrix(self._r)

    # -- shapes ----------------------------------------------------------------
    def resize_4x4(self):
        n = len(self._r)
        if n == 4:
            return self
        if n != 3:
            raise TypeError("can only resize 3x3 or 4x4")
        m = [list(r) + [0.0] for r in self._r]
        m.append([0.0, 0.0, 0.0, 1.0])
        self._r = m
        return self

    def to_3x3(self):
        if len(self._r) == 3:
            return self.copy()
        return Matrix([row[:3] for row in self._r[:3]])

    def transposed(self):
        n = len(self._r)
        return Matrix([[self._r[i][j] for i in range(n)] for j in range(n)])

    @property
    def T(self):
        return self.transposed()

    def inverted(self):
        # Gauss-Jordan with partial pivoting (works for 3x3 and 4x4)
        n = len(self._r)
        m = [list(row) + [1.0 if i == j else 0.0 for j in range(n)]
             for i, row in enumerate(self._r)]
        for c in range(n):
            p = max(range(c, n), key=lambda r: abs(m[r][c]))
            if abs(m[p][c]) < 1e-30:
                raise ZeroDivisionError("singular matrix")
            if p != c:
                m[c], m[p] = m[p], m[c]
            d = m[c][c]
            m[c] = [v / d for v in m[c]]
            for r in range(n):
                if r != c and m[r][c] != 0:
                    f = m[r][c]
                    m[r] = [a - f * b for a, b in zip(m[r], m[c])]
        return Matrix([row[n:] for row in m])

    @property
    def translation(self):
        n = len(self._r)
        return Vector(self._r[i][n - 1] for i in range(n - 1))

    def decompose(self):
        loc = self.translation.copy()
        scale = Vector([
            _m.sqrt(sum(self._r[k][0] ** 2 for k in range(3))),
            _m.sqrt(sum(self._r[k][1] ** 2 for k in range(3))),
            _m.sqrt(sum(self._r[k][2] ** 2 for k in range(3))),
        ])
        rot = Matrix([
            [self._r[i][0] / (scale[0] or 1), self._r[i][1] / (scale[1] or 1),
             self._r[i][2] / (scale[2] or 1)]
            for i in range(3)
        ])
        return loc, rot.to_quaternion(), scale

    def to_translation_euler_scale(self):
        loc, rot, scale = self.decompose()
        return loc, rot.to_euler(), scale

    def to_quaternion(self):
        # Shepperd's method (branchless numerically stable version)
        m = self.to_3x3()
        m00, m01, m02 = m._r[0][0], m._r[0][1], m._r[0][2]
        m10, m11, m12 = m._r[1][0], m._r[1][1], m._r[1][2]
        m20, m21, m22 = m._r[2][0], m._r[2][1], m._r[2][2]
        tr = m00 + m11 + m22
        if tr > 0:
            s = _m.sqrt(tr + 1.0) * 2
            w = 0.25 * s
            x = (m21 - m12) / s
            y = (m02 - m20) / s
            z = (m10 - m01) / s
        elif (m00 > m11) and (m00 > m22):
            s = _m.sqrt(1.0 + m00 - m11 - m22) * 2
            w = (m21 - m12) / s
            x = 0.25 * s
            y = (m01 + m10) / s
            z = (m02 + m20) / s
        elif m11 > m22:
            s = _m.sqrt(1.0 + m11 - m00 - m22) * 2
            w = (m02 - m20) / s
            x = (m01 + m10) / s
            y = 0.25 * s
            z = (m12 + m21) / s
        else:
            s = _m.sqrt(1.0 + m22 - m00 - m11) * 2
            w = (m10 - m01) / s
            x = (m02 + m20) / s
            y = (m12 + m21) / s
            z = 0.25 * s
        return Quaternion(w, x, y, z)

    # -- operators ----------------------------------------------------------------
    def __matmul__(self, o):
        if isinstance(o, Matrix):
            n = len(self._r)
            return Matrix([
                [sum(self._r[i][k] * o._r[k][j] for k in range(n)) for j in range(n)]
                for i in range(n)
            ])
        # Vector
        n = len(self._r)
        out = []
        for i in range(n):
            s = sum(self._r[i][k] * o[k] for k in range(min(n, len(o))))
            out.append(s)
        return Vector(out[:3] if len(o) == 3 else out)

    def __mul__(self, o):
        if isinstance(o, (Matrix, Vector)):
            return self.__matmul__(o)
        return Matrix([[a * o for a in r] for r in self._r])

    def __repr__(self):
        return "Matrix" + repr(self._r)


class _RowAccessor:
    def __init__(self, m):
        self._m = m

    def __getitem__(self, i):
        return Vector(self._m._r[i])

    def __setitem__(self, i, v):
        self._m._r[i] = [float(x) for x in v]


class _ColAccessor:
    def __init__(self, m):
        self._m = m

    def __getitem__(self, j):
        return Vector(self._m._r[i][j] for i in range(len(self._m._r)))

    def __setitem__(self, j, v):
        for i in range(len(self._m._r)):
            self._m._r[i][j] = float(v[i])
