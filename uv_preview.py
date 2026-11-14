"""Render a selected GLB mesh and its UV scrolling inside Tk."""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from io import BytesIO
from math import cos, pi, sin
from time import monotonic
from tkinter import ttk

from uv_export import can_replace_uv, embedded_floats, weight_channels

try:
    from uv_preview_gl import GLPreviewRenderer
except ImportError:
    GLPreviewRenderer = None

try:
    import numpy as np
    from PIL import Image, ImageTk
except ModuleNotFoundError as exc:
    if exc.name not in ("numpy", "PIL"):
        raise
    np = None


@dataclass
class Surface:
    positions: np.ndarray
    uv: np.ndarray
    triangles: np.ndarray
    pixels: np.ndarray
    wrap: tuple[int, int]
    cutoff: float


def read_accessor(source, index, width):
    accessor = source.model.accessors[index]
    if accessor.sparse is not None or accessor.bufferView is None:
        raise ValueError("Sparse geometry is unsupported in the preview.")
    view = source.model.bufferViews[accessor.bufferView]
    if view.buffer != 0:
        raise ValueError("Preview geometry must be embedded in the GLB.")
    dtype = np.dtype({
        5120: "i1", 5121: "u1", 5122: "<i2", 5123: "<u2", 5125: "<u4", 5126: "<f4",
    }[accessor.componentType])
    offset = (view.byteOffset or 0) + (accessor.byteOffset or 0)
    stride = view.byteStride or dtype.itemsize * width
    relative_offset = accessor.byteOffset or 0
    if (accessor.count <= 0 or stride < dtype.itemsize * width
            or relative_offset + stride * (accessor.count - 1) + dtype.itemsize * width > view.byteLength):
        raise ValueError("The preview found an invalid geometry accessor.")
    values = np.ndarray(
        (accessor.count, width), dtype=dtype, buffer=source.get_glb_resource().data,
        offset=offset, strides=(stride, dtype.itemsize),
    )
    if accessor.normalized and dtype.kind in "iu":
        return np.maximum(-1, values / np.iinfo(dtype).max)
    return values


def load_surface(source, primitive):
    model = source.model
    if primitive.mode not in (None, 4):
        raise ValueError("The preview supports triangle primitives.")
    positions = read_accessor(source, primitive.attributes.POSITION, 3)
    uv_index = primitive.attributes.TEXCOORD_0
    uv = read_accessor(source, uv_index, 2) if uv_index is not None else np.zeros((len(positions), 2))
    indices = (np.arange(len(positions)) if primitive.indices is None
               else read_accessor(source, primitive.indices, 1).ravel())
    if (indices.dtype.kind not in "iu" or not len(indices) or indices.min() < 0
            or indices.max() >= len(positions) or len(positions) != len(uv)
            or not np.isfinite(positions).all() or not np.isfinite(uv).all()):
        raise ValueError("The preview found invalid mesh data.")
    wrap = (10497, 10497)
    factor = [0.68, 0.72, 0.78, 1.0]
    cutoff = 0.0
    pixels = np.full((1, 1, 4), 255, dtype=np.uint8)
    if primitive.material is not None:
        material = model.materials[primitive.material]
        pbr = material.pbrMetallicRoughness
        if material.alphaMode == "MASK":
            cutoff = material.alphaCutoff if material.alphaCutoff is not None else 0.5
        if pbr is not None:
            factor = pbr.baseColorFactor or [1.0] * 4
        if pbr is not None and pbr.baseColorTexture is not None:
            info = pbr.baseColorTexture
            if info.texCoord not in (None, 0) or uv_index is None or info.extensions:
                raise ValueError("The preview needs a base-color texture using TEXCOORD_0 directly.")
            texture = model.textures[info.index]
            if texture.source is None:
                raise ValueError("This texture format is unsupported in the preview.")
            image = model.images[texture.source]
            if image.bufferView is None:
                raise ValueError("Preview textures must be embedded in the GLB.")
            view = model.bufferViews[image.bufferView]
            if view.buffer != 0:
                raise ValueError("Preview textures must be embedded in the GLB.")
            start = view.byteOffset or 0
            data = source.get_glb_resource().data[start:start + view.byteLength]
            with Image.open(BytesIO(data)) as decoded:
                decoded.thumbnail((1024, 1024))
                pixels = np.asarray(decoded.convert("RGBA"))
            if texture.sampler is not None:
                sampler = model.samplers[texture.sampler]
                wrap = (sampler.wrapS or 10497, sampler.wrapT or 10497)
    return Surface(positions, uv, indices.reshape(-1, 3),
                   (pixels * factor).astype(np.uint8), wrap, cutoff)


def saved_uv_motion(source, node_index):
    model = source.model
    node = model.nodes[node_index]
    mesh = model.meshes[node.mesh]
    weights = node.weights or mesh.weights or [0.0] * len(mesh.primitives[0].targets or [])
    existing = list(weight_channels(model.animations or [], node_index, node.mesh))
    if not existing:
        return None
    if len(existing) != 1 or not can_replace_uv(source, mesh, weights, *existing[0]):
        raise ValueError("This saved weight animation is unsupported in the preview.")
    animation, channel = existing[0]
    sampler = animation.samplers[channel.sampler]
    if sampler.interpolation not in (None, "LINEAR", "STEP"):
        raise ValueError("This saved animation uses unsupported interpolation.")
    times = np.asarray(embedded_floats(source, sampler.input, "SCALAR")).ravel()
    values = np.asarray(embedded_floats(source, sampler.output, "SCALAR")).ravel()
    if (not len(times) or values.size != len(times) * len(weights) or times[-1] <= 0
            or not np.isfinite(times).all() or not np.isfinite(values).all() or np.any(np.diff(times) <= 0)):
        raise ValueError("This saved animation has invalid keyframes.")
    offsets = values.reshape(-1, len(weights))[:, [-1, -2]]

    def sample(elapsed):
        time = elapsed % times[-1]
        if sampler.interpolation == "STEP":
            return offsets[max(0, np.searchsorted(times, time, side="right") - 1)]
        return [np.interp(time, times, offsets[:, axis]) for axis in (0, 1)]

    return sample


def node_transform(model, index):
    chain = []
    parents = {child: i for i, node in enumerate(model.nodes) for child in node.children or []}
    while index is not None:
        if index in chain:
            raise ValueError("The node hierarchy has a cycle.")
        chain.append(index)
        index = parents.get(index)
    world = np.eye(4)
    for i in reversed(chain):
        node = model.nodes[i]
        if node.matrix is not None:
            local = np.asarray(node.matrix).reshape((4, 4), order="F")
        else:
            x, y, z, w = node.rotation or (0, 0, 0, 1)
            local = np.eye(4)
            local[:3, :3] = np.array([
                [1 - 2*(y*y + z*z), 2*(x*y - z*w), 2*(x*z + y*w)],
                [2*(x*y + z*w), 1 - 2*(x*x + z*z), 2*(y*z - x*w)],
                [2*(x*z - y*w), 2*(y*z + x*w), 1 - 2*(x*x + y*y)],
            ]) * (node.scale or (1, 1, 1))
            local[:3, 3] = node.translation or (0, 0, 0)
        world = world @ local
    return world


def view_rotation(yaw, pitch):
    rotation_y = np.array([[cos(yaw), 0, sin(yaw)], [0, 1, 0], [-sin(yaw), 0, cos(yaw)]])
    rotation_x = np.array([[1, 0, 0], [0, cos(pitch), -sin(pitch)], [0, sin(pitch), cos(pitch)]])
    return rotation_x @ rotation_y


def project_surfaces(surfaces, width, height, rotation, scale, pan):
    projected = [surface.positions @ rotation.T for surface in surfaces]
    depth = np.full((height, width), -np.inf, dtype=np.float32)
    material = np.full((height, width), -1, dtype=np.int32)
    uv_map = np.zeros((height, width, 2), dtype=np.float32)
    pixel_x = np.arange(width, dtype=np.float32)[None, :] + 0.5
    pixel_y = np.arange(height, dtype=np.float32)[:, None] + 0.5
    for index, (surface, vertices) in enumerate(zip(surfaces, projected)):
        triangles = ((vertices * (scale, -scale, 1)
                      + (width * (1 + pan[0]) / 2, height * (1 - pan[1]) / 2, 0))
                     .astype(np.float32)[surface.triangles])
        lower = np.maximum(0, np.floor(triangles[:, :, :2].min(axis=1))).astype(int)
        upper = np.minimum((width, height), np.ceil(triangles[:, :, :2].max(axis=1))).astype(int)
        a, b, c = triangles.transpose(1, 0, 2)
        ab, ac = b - a, c - a
        area = ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0]
        valid = (np.abs(area) >= 1e-8) & (lower < upper).all(axis=1)
        for i in np.flatnonzero(valid):
            left, top = lower[i]
            right, bottom = upper[i]
            vertex, edge_b, edge_c = a[i], ab[i], ac[i]
            x, y = pixel_x[:, left:right] - vertex[0], pixel_y[top:bottom, :] - vertex[1]
            wb = (x * edge_c[1] - y * edge_c[0]) / area[i]
            wc = (edge_b[0] * y - edge_b[1] * x) / area[i]
            z = vertex[2] + wb * edge_b[2] + wc * edge_c[2]
            region = np.s_[top:bottom, left:right]
            visible = (wb >= 0) & (wc >= 0) & (wb + wc <= 1) & (z > depth[region])
            if visible.any():
                uv_a, uv_b, uv_c = surface.uv[surface.triangles[i]]
                uv_map[region][visible] = (
                    uv_a + wb[visible, None] * (uv_b - uv_a) + wc[visible, None] * (uv_c - uv_a)
                )
                depth[region][visible] = z[visible]
                material[region][visible] = index
    return [(material == index, uv_map[material == index]) for index in range(len(surfaces))]


def texel(coordinate, size, wrap):
    if wrap == 10497:
        coordinate = coordinate % 1
    elif wrap == 33648:
        coordinate = 1 - abs(coordinate % 2 - 1)
    return np.clip(np.floor(coordinate * size), 0, size - 1).astype(np.intp)


class UVPreview(ttk.LabelFrame):
    def __init__(self, parent):
        super().__init__(parent, text="UV Preview", padding=8)
        self.canvas = tk.Canvas(self, width=420, height=300, background="#17191e", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.status = ttk.Label(self, wraplength=400)
        self.status.pack(anchor="w", pady=(6, 0))
        self.surfaces = []
        self.projection = None
        self.timer = None
        self.photo = None
        self.renderer = None
        self.renderer_checked = False
        self.image_item = self.canvas.create_image(0, 0)
        self.canvas.bind("<Configure>", self.invalidate_projection)
        self.canvas.bind("<Button-1>", self.start_drag)
        self.canvas.bind("<B1-Motion>", self.orbit)
        self.canvas.bind("<ButtonRelease-1>", self.end_drag)
        self.canvas.bind("<Button-3>", self.start_drag)
        self.canvas.bind("<B3-Motion>", self.pan_view)
        self.canvas.bind("<ButtonRelease-3>", self.end_drag)
        self.canvas.bind("<MouseWheel>", self.mouse_zoom)
        self.canvas.bind("<Button-4>", lambda event: self.zoom_by(1.2))
        self.canvas.bind("<Button-5>", lambda event: self.zoom_by(1 / 1.2))
        controls = ttk.Frame(self)
        controls.pack(anchor="e", pady=(4, 0))
        ttk.Button(controls, text="Zoom −", command=lambda: self.zoom_by(1 / 1.2)).pack(side="left")
        ttk.Button(controls, text="Zoom +", command=lambda: self.zoom_by(1.2)).pack(side="left", padx=4)
        ttk.Button(controls, text="Reset View", command=self.reset_view).pack(side="left")
        ttk.Button(controls, text="Show Saved", command=self.use_saved).pack(side="left", padx=(4, 0))
        self.bind("<Destroy>", self.close)
        self.show_node(None, None)

    def show_node(self, source, index):
        self.stop()
        self.canvas.itemconfigure(self.image_item, image="")
        self.photo = None
        self.surfaces = []
        self.projection = None
        self.reset_view()
        if self.renderer is not None:
            self.renderer.clear_surfaces()
        self.saved = self.motion = None
        self.saved_status = "Saved model"
        if np is None:
            self.status.configure(text="Install preview dependencies with python -m pip install -r requirements.txt.")
            return
        if index is None:
            self.status.configure(text="Open a GLB and select a mesh to preview.")
            return
        try:
            mesh = source.model.meshes[source.model.nodes[index].mesh]
            self.surfaces = [load_surface(source, primitive) for primitive in mesh.primitives]
            transform = node_transform(source.model, index)
            for surface in self.surfaces:
                surface.positions = surface.positions @ transform[:3, :3].T + transform[:3, 3]
            points = np.concatenate([surface.positions for surface in self.surfaces])
            pivot = (points.min(axis=0) + points.max(axis=0)) / 2
            self.radius = np.linalg.norm(points - pivot, axis=1).max()
            for surface in self.surfaces:
                surface.positions = surface.positions - pivot
        except (AttributeError, IndexError, KeyError, OSError, TypeError, ValueError) as exc:
            self.surfaces = []
            self.status.configure(text=f"Preview unavailable: {exc}")
            return
        if not self.renderer_checked:
            self.renderer_checked = True
            if GLPreviewRenderer is not None:
                try:
                    self.renderer = GLPreviewRenderer()
                except Exception:
                    pass
        if self.renderer is not None:
            self.renderer.set_surfaces(self.surfaces)
        try:
            self.saved = saved_uv_motion(source, index)
            if self.saved is not None:
                self.saved_status = "Saved UV animation"
        except (AttributeError, IndexError, TypeError, ValueError) as exc:
            self.saved_status = str(exc)
        self.use_saved()

    def use_saved(self):
        if self.surfaces:
            self.set_motion(self.saved, self.saved_status)

    def show_settings(self, entry):
        if not self.surfaces:
            return
        if entry.speed == 0:
            self.set_motion(None, "Preview paused · Enter a nonzero speed")
            return
        try:
            duration, x, y = entry.keyframes()
        except ValueError as exc:
            self.invalid_settings(str(exc))
            return
        displacement = np.array((x, y))
        self.set_motion(lambda time: (time % duration / duration) * displacement,
                        "Edited settings")

    def invalid_settings(self, message):
        if self.surfaces:
            self.set_motion(None, message)

    def set_motion(self, motion, status):
        self.motion = motion
        self.started = monotonic()
        backend = "OpenGL" if self.renderer is not None else "CPU fallback"
        self.status.configure(text=f"{status} · {backend}")
        self.request_frame()

    def invalidate_projection(self, event=None):
        was_cached = self.projection is not None
        self.projection = None
        if was_cached:
            self.stop()
        self.request_frame()

    def start_drag(self, event):
        self.drag = (event.x, event.y)

    def orbit(self, event):
        if self.drag is None:
            self.start_drag(event)
            return
        # The front of the mesh moves with the pointer. Limit pitch to keep it upright.
        self.yaw += (event.x - self.drag[0]) * 0.008
        self.pitch = max(-pi / 2, min(pi / 2, self.pitch + (event.y - self.drag[1]) * 0.008))
        self.start_drag(event)
        self.invalidate_projection()

    def end_drag(self, event):
        if self.drag is not None:
            self.drag = None
            self.invalidate_projection()

    def pan_view(self, event):
        if self.drag is not None:
            self.pan[0] += 2 * (event.x - self.drag[0]) / max(1, self.canvas.winfo_width())
            self.pan[1] -= 2 * (event.y - self.drag[1]) / max(1, self.canvas.winfo_height())
        self.start_drag(event)
        self.invalidate_projection()

    def zoom_by(self, factor):
        self.zoom = min(20.0, max(0.15, self.zoom * factor))
        self.invalidate_projection()

    def mouse_zoom(self, event):
        if event.delta:
            self.zoom_by(1.2 if event.delta > 0 else 1 / 1.2)

    def reset_view(self):
        self.yaw, self.pitch = 0.0, 0.0
        self.pan = [0.0, 0.0]
        self.zoom = 1.0
        self.drag = None
        self.invalidate_projection()

    def request_frame(self):
        if self.surfaces and self.timer is None:
            self.timer = self.after(16, self.draw)

    def draw(self):
        self.timer = None
        frame_started = monotonic()
        canvas_width, canvas_height = self.canvas.winfo_width(), self.canvas.winfo_height()
        resolution = 960 if self.renderer is not None else (180 if self.drag is not None else 420)
        scale = min(1, resolution / max(canvas_width, canvas_height))
        width, height = max(1, int(canvas_width * scale)), max(1, int(canvas_height * scale))
        radius = max(self.radius, 1e-6)
        pixels_per_unit = 0.44 * min(width, height) * self.zoom / radius
        rotation = view_rotation(self.yaw, self.pitch)
        offset = self.motion(monotonic() - self.started) if self.motion else (0, 0)
        if self.renderer is not None:
            image = self.renderer.draw(
                (width, height), rotation, (2*pixels_per_unit/width, 2*pixels_per_unit/height, -0.9/radius),
                tuple(self.pan), offset,
            )
        else:
            image = self.draw_cpu(width, height, rotation, pixels_per_unit, offset)
        if image.size != (canvas_width, canvas_height):
            image = image.resize((canvas_width, canvas_height), Image.Resampling.BILINEAR)
        self.photo = ImageTk.PhotoImage(image, master=self.canvas)
        self.canvas.coords(self.image_item, canvas_width / 2, canvas_height / 2)
        self.canvas.itemconfigure(self.image_item, image=self.photo)
        if self.motion:
            interval = 16 if self.renderer is not None else 33
            delay = max(1, interval - int((monotonic() - frame_started) * 1000))
            self.timer = self.after(delay, self.draw)

    def draw_cpu(self, width, height, rotation, scale, offset):
        if self.projection is None:
            self.projection = project_surfaces(self.surfaces, width, height, rotation, scale, self.pan)
        frame = np.empty((height, width, 3), dtype=np.uint8)
        frame[:] = (23, 25, 30)
        for surface, (mask, uv) in zip(self.surfaces, self.projection):
            u = texel(uv[:, 0] + offset[0], surface.pixels.shape[1], surface.wrap[0])
            v = texel(uv[:, 1] + offset[1], surface.pixels.shape[0], surface.wrap[1])
            color = surface.pixels[v, u]
            frame[mask] = np.where(color[:, 3:] >= surface.cutoff * 255, color[:, :3], (23, 25, 30))
        return Image.fromarray(frame)

    def close(self, event):
        self.stop()
        if self.renderer is not None:
            self.renderer.close()

    def stop(self, event=None):
        if self.timer is not None:
            self.after_cancel(self.timer)
            self.timer = None
