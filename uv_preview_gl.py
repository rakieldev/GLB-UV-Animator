"""OpenGL renderer for the Tk UV preview."""

import moderngl
import numpy as np
from PIL import Image


class GLPreviewRenderer:
    def __init__(self):
        self.context = moderngl.create_context(standalone=True, require=330)
        self.renderer_name = self.context.info["GL_RENDERER"]
        self.context.enable(moderngl.DEPTH_TEST)
        self.framebuffer = None
        self.meshes = []
        self.program = self.context.program(
            vertex_shader="""
                #version 330
                in vec3 position;
                in vec2 uv;
                uniform mat3 rotation;
                uniform vec3 scale;
                uniform vec2 pan;
                out vec2 texcoord;
                void main() {
                    vec3 point = (rotation * position) * scale;
                    gl_Position = vec4(point.xy + pan, point.z, 1.0);
                    texcoord = uv;
                }
            """,
            fragment_shader="""
                #version 330
                uniform sampler2D base_color;
                uniform vec2 offset;
                uniform ivec2 wrap;
                uniform float cutoff;
                in vec2 texcoord;
                out vec4 color;
                float wrap_axis(float value, int mode) {
                    if (mode == 10497) return fract(value);
                    if (mode == 33648) return 1.0 - abs(mod(value, 2.0) - 1.0);
                    return clamp(value, 0.0, 1.0);
                }
                void main() {
                    vec2 uv = texcoord + offset;
                    color = texture(base_color, vec2(wrap_axis(uv.x, wrap.x), wrap_axis(uv.y, wrap.y)));
                    if (color.a < cutoff) discard;
                }
            """,
        )

    def set_surfaces(self, surfaces):
        self.clear_surfaces()
        for surface in surfaces:
            data = np.column_stack((surface.positions, surface.uv)).astype("f4")
            vertices = self.context.buffer(data.tobytes())
            indices = self.context.buffer(surface.triangles.astype("u4").tobytes())
            vao = self.context.vertex_array(
                self.program, [(vertices, "3f 2f", "position", "uv")], indices,
            )
            height, width = surface.pixels.shape[:2]
            texture = self.context.texture((width, height), 4, surface.pixels.tobytes())
            texture.repeat_x = texture.repeat_y = False
            self.meshes.append((vao, texture, vertices, indices, surface.wrap, surface.cutoff))

    def clear_surfaces(self):
        for vao, texture, vertices, indices, _, _ in self.meshes:
            for resource in (vao, texture, vertices, indices):
                resource.release()
        self.meshes.clear()

    def draw(self, size, rotation, scale, pan, offset):
        if self.framebuffer is None or self.framebuffer.size != size:
            if self.framebuffer is not None:
                self.framebuffer.release()
            self.framebuffer = self.context.simple_framebuffer(size, components=3)
        self.framebuffer.use()
        self.framebuffer.clear(23 / 255, 25 / 255, 30 / 255)
        self.program["rotation"].write(rotation.T.astype("f4").tobytes())
        self.program["scale"].value = scale
        self.program["pan"].value = pan
        self.program["offset"].value = tuple(offset)
        for vao, texture, _, _, wrap, cutoff in self.meshes:
            texture.use()
            self.program["wrap"].value = wrap
            self.program["cutoff"].value = cutoff
            vao.render(moderngl.TRIANGLES)
        return Image.frombytes("RGB", size, self.framebuffer.read(components=3), "raw", "RGB", 0, -1)

    def close(self):
        self.clear_surfaces()
        if self.framebuffer is not None:
            self.framebuffer.release()
        self.program.release()
        self.context.release()
