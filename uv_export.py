"""Add Ikemen GO UV animations to a binary glTF model."""

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from struct import pack, unpack, unpack_from
from tempfile import NamedTemporaryFile

from gltflib import (
    Accessor, Animation, AnimationSampler, Attributes, BufferTarget, BufferView,
    Channel, ComponentType, GLTF, Target,
)


# --- Core Logic ---

def float32(value):
    try:
        rounded, = unpack("<f", pack("<f", value))
    except OverflowError as exc:
        raise ValueError("Animation values must fit in a 32-bit float.") from exc
    if not isfinite(rounded):
        raise ValueError("Animation values must fit in a finite 32-bit float.")
    return rounded


@dataclass(frozen=True)
class UVAnimation:
    node_index: int
    speed: float
    x: float
    y: float

    def keyframes(self):
        if not all(isfinite(value) for value in (self.speed, self.x, self.y)):
            raise ValueError("Speed, X, and Y must be finite numbers.")
        if self.speed == 0:
            raise ValueError("Speed cannot be zero.")
        duration = float32(1.0 / abs(self.speed))
        if duration == 0:
            raise ValueError("Speed is too high to represent a positive duration.")
        sign = 1 if self.speed > 0 else -1
        return duration, float32(sign * self.x), float32(sign * self.y)


class BufferWriter:
    def __init__(self, gltf):
        self.model = gltf.model
        self.resource = gltf.get_glb_resource()
        if not self.model.buffers or self.model.buffers[0].uri is not None or self.resource is None:
            raise ValueError("The GLB needs an embedded binary buffer.")
        self.buffer = self.model.buffers[0]
        data = self.resource.data
        size = self.buffer.byteLength
        if not 0 <= len(data) - size <= 3:
            raise ValueError("The embedded binary buffer has an invalid length.")
        # Binary Injection: keep the declared bytes and align new data to four bytes.
        self.data = bytearray(data[:size])
        self.data.extend(bytes(-size % 4))
        self.model.bufferViews = self.model.bufferViews or []
        self.model.accessors = self.model.accessors or []
        self.uv_targets = {}

    def append_accessor(self, payload, count, value_type, minimum, maximum, target=None):
        # Buffer Views & Accessors
        view_index = len(self.model.bufferViews)
        self.model.bufferViews.append(BufferView(
            buffer=0, byteOffset=len(self.data), byteLength=len(payload), target=target,
        ))
        self.data.extend(payload)
        accessor_index = len(self.model.accessors)
        self.model.accessors.append(Accessor(
            bufferView=view_index, componentType=ComponentType.FLOAT.value,
            count=count, type=value_type, min=minimum, max=maximum,
        ))
        return accessor_index

    def uv_accessors(self, count):
        if count not in self.uv_targets:
            # The legacy format stores the V target before the U target.
            self.uv_targets[count] = tuple(
                self.append_accessor(
                    pack("<2f", *delta) * count, count, "VEC2", list(delta), list(delta),
                    BufferTarget.ARRAY_BUFFER.value,
                )
                for delta in ((0.0, 1.0), (1.0, 0.0))
            )
        return self.uv_targets[count]

    def scalars(self, values):
        return self.append_accessor(
            pack(f"<{len(values)}f", *values), len(values), "SCALAR",
            [min(values)], [max(values)],
        )

    def finish(self):
        self.resource.data = bytes(self.data)
        self.buffer.byteLength = len(self.data)


def weight_channels(animations, node_index, mesh_index):
    for animation in animations:
        for channel in animation.channels:
            target = channel.target
            if target.node == node_index and target.path == "weights":
                yield animation, channel
                continue
            pointer = (target.extensions or {}).get("KHR_animation_pointer", {}).get("pointer", "")
            for path in (f"/nodes/{node_index}/weights", f"/meshes/{mesh_index}/weights"):
                if pointer == path or pointer.startswith(path + "/"):
                    yield animation, channel
                    break


def has_weight_animation(animations, node_index, mesh_index):
    return next(weight_channels(animations, node_index, mesh_index), None) is not None


def embedded_floats(gltf, index, value_type):
    accessor = gltf.model.accessors[index]
    if (accessor.type != value_type or accessor.componentType != ComponentType.FLOAT.value
            or accessor.sparse is not None or accessor.bufferView is None):
        return ()
    view = gltf.model.bufferViews[accessor.bufferView]
    width = 2 if value_type == "VEC2" else 1
    size = width * 4
    stride = view.byteStride or size
    offset = accessor.byteOffset or 0
    if (view.buffer != 0 or accessor.count <= 0 or stride < size
            or offset + (accessor.count - 1) * stride + size > view.byteLength):
        return ()
    data = gltf.get_glb_resource().data
    start = (view.byteOffset or 0) + offset
    if start + (accessor.count - 1) * stride + size > len(data):
        return ()
    return tuple(
        unpack_from(f"<{width}f", data, start + i * stride)
        for i in range(accessor.count)
    )


def can_replace_uv(gltf, mesh, weights, animation, channel):
    if channel.target.path != "weights" or len(animation.channels) != 1 or len(weights) < 2:
        return False
    for primitive in mesh.primitives:
        uv_index = primitive.attributes.TEXCOORD_0
        if uv_index is None:
            return False
        count = gltf.model.accessors[uv_index].count
        for target, delta in zip(primitive.targets[-2:], ((0.0, 1.0), (1.0, 0.0))):
            if target.TEXCOORD_0 is None or any(
                value is not None for key, value in vars(target).items() if key != "TEXCOORD_0"
            ):
                return False
            values = embedded_floats(gltf, target.TEXCOORD_0, "VEC2")
            if len(values) != count or any(value != delta for value in values):
                return False

    # Earlier morph targets may coexist if the clip leaves their weights static.
    if len(weights) == 2:
        return True
    sampler = animation.samplers[channel.sampler]
    if sampler.interpolation not in (None, "LINEAR", "STEP"):
        return False
    values = embedded_floats(gltf, sampler.output, "SCALAR")
    count = len(weights)
    static = tuple((float32(value),) for value in weights[:-2])
    return bool(values) and len(values) % count == 0 and all(
        values[offset:offset + len(static)] == static for offset in range(0, len(values), count)
    )


def add_uv_animations(source, animations):
    """Return an edited copy of the loaded GLB so exports can be retried."""
    animations = tuple(animations)
    if not animations:
        raise ValueError("Add at least one animation to the queue.")
    if len({entry.node_index for entry in animations}) != len(animations):
        raise ValueError("Each node can have one UV animation in the queue.")

    gltf = source.clone()
    model = gltf.model
    writer = BufferWriter(gltf)
    model.animations = model.animations or []
    nodes = model.nodes or []
    mesh_users = Counter(node.mesh for node in nodes)
    for entry in animations:
        # Find Node by its index in the loaded GLB.
        index = entry.node_index
        if not 0 <= index < len(nodes) or nodes[index].mesh is None:
            raise ValueError(f"Node {index} has no mesh.")
        node = nodes[index]
        label = f"Node {index}" + (f" ({node.name})" if node.name else "")
        mesh = model.meshes[node.mesh]
        target_counts = {len(primitive.targets or []) for primitive in mesh.primitives}
        if len(target_counts) != 1:
            raise ValueError(f"{label} must have the same morph target count in every primitive.")
        target_count = target_counts.pop()
        defaults = mesh.weights if mesh.weights is not None else [0.0] * target_count
        weights = node.weights if node.weights is not None else defaults
        if len(defaults) != target_count or len(weights) != target_count:
            raise ValueError(f"{label} has an invalid number of morph weights.")

        duration, x, y = entry.keyframes()
        existing = list(weight_channels(model.animations, index, node.mesh))
        if existing:
            if len(existing) != 1 or not can_replace_uv(gltf, mesh, weights, *existing[0]):
                raise ValueError(f"{label} has a morph weight animation that cannot be replaced as UV scrolling.")
            animation, channel = existing[0]
            time_accessor = writer.scalars([0.0, duration])
            static = [float32(value) for value in weights[:-2]]
            weight_accessor = writer.scalars([*static, 0.0, 0.0, *static, y, x])
            animation.samplers[channel.sampler] = AnimationSampler(
                input=time_accessor, output=weight_accessor, interpolation="LINEAR",
            )
            continue

        if mesh_users[node.mesh] > 1:
            mesh = deepcopy(mesh)
            node.mesh = len(model.meshes)
            model.meshes.append(mesh)

        # Prepare Morph Targets
        for primitive in mesh.primitives:
            uv_index = primitive.attributes.TEXCOORD_0
            if uv_index is None:
                raise ValueError(f"{label} needs TEXCOORD_0 in every primitive.")
            uv = model.accessors[uv_index]
            if uv.type != "VEC2" or uv.count <= 0:
                raise ValueError(f"{label} has invalid TEXCOORD_0 data.")
            primitive.targets = [
                *(primitive.targets or []),
                *(Attributes(TEXCOORD_0=accessor) for accessor in writer.uv_accessors(uv.count)),
            ]

        mesh.weights = [*defaults, 0.0, 0.0]
        node.weights = [*weights, 0.0, 0.0]
        names = (mesh.extras or {}).get("targetNames") if isinstance(mesh.extras, dict) else None
        if isinstance(names, list) and len(names) == target_count:
            mesh.extras["targetNames"] = [*names, "UV V", "UV U"]
        # Animation Data
        time_accessor = writer.scalars([0.0, duration])
        weight_accessor = writer.scalars([*node.weights, *weights, y, x])
        model.animations.append(Animation(
            name="UV_Speed_Anim",
            channels=[Channel(sampler=0, target=Target(node=index, path="weights"))],
            samplers=[AnimationSampler(input=time_accessor, output=weight_accessor, interpolation="LINEAR")],
        ))

    writer.finish()
    return gltf


def export_glb(source, output_file, animations):
    # Export using the specific output path provided by user
    destination = Path(output_file)
    if destination.suffix.lower() != ".glb":
        raise ValueError("The output file must have a .glb extension.")
    gltf = add_uv_animations(source, animations)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=destination.parent, suffix=".glb", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        gltf.export_glb(str(temporary), save_file_resources=False)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
