import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gltflib import GLTF

from uv_export import UVAnimation, export_glb


def glb_chunk(kind, data, padding):
    data += padding * (-len(data) % 4)
    return struct.pack("<I4s", len(data), kind) + data


def write_model(path, *, shared=False, multiple=False, morph=False, padded=False):
    positions = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
    uv = struct.pack("<6f", 0, 0, 1, 0, 0, 1)
    payload = positions + uv
    primitive = {"attributes": {"POSITION": 0, "TEXCOORD_0": 1}}
    nodes = [{"name": "Water", "mesh": 0}]
    if shared:
        nodes.append({"name": "Water", "mesh": 0})
    mesh = {"primitives": [primitive] * (2 if multiple else 1)}
    model = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "nodes": nodes,
        "meshes": [mesh],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(positions)},
            {"buffer": 0, "byteOffset": len(positions), "byteLength": len(uv)},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3",
             "min": [0, 0, 0], "max": [1, 1, 0]},
            {"bufferView": 1, "componentType": 5126, "count": 3, "type": "VEC2"},
        ],
    }
    if morph:
        mesh.update(weights=[0.25], extras={"targetNames": ["Raise"]})
        primitive["targets"] = [{"POSITION": 2}]
        model["accessors"].append({
            "componentType": 5126, "count": 3, "type": "VEC3",
            "min": [0, 0, 0], "max": [0, 0, 0],
        })
    if padded:
        payload += b"\x01"
    model["buffers"] = [{"byteLength": len(payload)}]
    chunks = glb_chunk(b"JSON", json.dumps(model).encode(), b" ")
    chunks += glb_chunk(b"BIN\x00", payload, b"\x00")
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, len(chunks) + 12) + chunks)


def accessor_bytes(gltf, index):
    accessor = gltf.model.accessors[index]
    view = gltf.model.bufferViews[accessor.bufferView]
    return gltf.get_glb_resource().data[view.byteOffset:view.byteOffset + view.byteLength]


class UVExportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.input = Path(directory.name, "source.glb")
        self.output = Path(directory.name, "animated.glb")

    def export(self, *entries, **model_options):
        write_model(self.input, **model_options)
        source = GLTF.load_glb(str(self.input))
        export_glb(source, self.output, entries)
        return source, GLTF.load_glb(str(self.output))

    def test_legacy_animation_data(self):
        source, result = self.export(UVAnimation(0, -2, 0.25, -0.5))
        animation, = result.model.animations
        sampler, = animation.samplers
        channel, = animation.channels
        self.assertEqual(animation.name, "UV_Speed_Anim")
        self.assertEqual(sampler.interpolation, "LINEAR")
        self.assertEqual((channel.target.node, channel.target.path), (0, "weights"))
        self.assertEqual(accessor_bytes(result, sampler.input), struct.pack("<2f", 0, 0.5))
        self.assertEqual(accessor_bytes(result, sampler.output), struct.pack("<4f", 0, 0, 0.5, -0.25))
        v, u = result.model.meshes[0].primitives[0].targets
        self.assertEqual(accessor_bytes(result, v.TEXCOORD_0), struct.pack("<2f", 0, 1) * 3)
        self.assertEqual(accessor_bytes(result, u.TEXCOORD_0), struct.pack("<2f", 1, 0) * 3)
        self.assertIsNone(source.model.animations)
        self.assertEqual(result.model.nodes[0].weights, [0.0, 0.0])

    def test_duplicate_names_shared_mesh_and_multiple_primitives(self):
        _, result = self.export(UVAnimation(1, 1, 1, 0), shared=True, multiple=True, morph=True)
        first, selected = result.model.nodes
        self.assertNotEqual(first.mesh, selected.mesh)
        original_mesh, animated_mesh = result.model.meshes
        self.assertEqual(original_mesh.weights, [0.25])
        self.assertEqual(animated_mesh.weights, [0.25, 0.0, 0.0])
        for primitive in animated_mesh.primitives:
            self.assertEqual(len(primitive.targets), 3)
            self.assertEqual(primitive.targets[0].POSITION, 2)
        self.assertEqual([len(p.targets) for p in original_mesh.primitives], [1, 1])
        self.assertEqual(animated_mesh.extras["targetNames"], ["Raise", "UV V", "UV U"])
        animation, = result.model.animations
        self.assertEqual(animation.channels[0].target.node, 1)
        self.assertEqual(accessor_bytes(result, animation.samplers[0].output),
                         struct.pack("<6f", 0.25, 0, 0, 0.25, 0, 1))

    def test_padded_bin_uses_aligned_offsets(self):
        _, result = self.export(UVAnimation(0, 1, 1, 0), padded=True)
        target = result.model.meshes[0].primitives[0].targets[0]
        view = result.model.bufferViews[result.model.accessors[target.TEXCOORD_0].bufferView]
        self.assertEqual(view.byteOffset, 64)
        self.assertEqual(accessor_bytes(result, target.TEXCOORD_0), struct.pack("<2f", 0, 1) * 3)
        self.assertEqual(result.get_glb_resource().data[60:64], b"\x01\x00\x00\x00")

    def test_replaces_uv_animation_and_rejects_duplicate_queue_entries(self):
        source, result = self.export(UVAnimation(0, 1, 1, 0))
        entry = UVAnimation(0, 1, 1, 0)
        export_glb(result, self.output, [UVAnimation(0, 2, 0, -1)])
        replaced = GLTF.load_glb(str(self.output))
        animation, = replaced.model.animations
        sampler, = animation.samplers
        self.assertEqual(accessor_bytes(replaced, sampler.input), struct.pack("<2f", 0, 0.5))
        self.assertEqual(accessor_bytes(replaced, sampler.output), struct.pack("<4f", 0, 0, -1, 0))
        self.assertEqual(replaced.model.meshes, result.model.meshes)
        with self.assertRaisesRegex(ValueError, "one UV animation"):
            export_glb(source, self.output, [entry, entry])

    def test_keeps_weight_animation_with_geometric_morph(self):
        _, result = self.export(UVAnimation(0, 1, 1, 0), morph=True)
        primitive = result.model.meshes[0].primitives[0]
        primitive.targets[-1].POSITION = primitive.targets[0].POSITION
        original = self.output.read_bytes()
        with self.assertRaisesRegex(ValueError, "cannot be replaced as UV scrolling"):
            export_glb(result, self.output, [UVAnimation(0, 2, 0, 1)])
        self.assertEqual(self.output.read_bytes(), original)

    def test_failed_write_keeps_previous_output(self):
        source, _ = self.export(UVAnimation(0, 1, 1, 0))
        previous = self.output.read_bytes()
        with patch.object(GLTF, "export_glb", side_effect=OSError("write failed")):
            with self.assertRaisesRegex(OSError, "write failed"):
                export_glb(source, self.output, [UVAnimation(0, 2, 0, 1)])
        self.assertEqual(self.output.read_bytes(), previous)
        self.assertCountEqual(self.output.parent.iterdir(), [self.input, self.output])

    def test_invalid_numbers_are_rejected(self):
        for speed, x in ((0, 1), (float("nan"), 1), (1, float("inf")), (1e300, 1), (1e-300, 1)):
            with self.subTest(speed=speed, x=x), self.assertRaises(ValueError):
                UVAnimation(0, speed, x, 0).keyframes()


if __name__ == "__main__":
    unittest.main()
