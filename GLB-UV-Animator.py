import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import struct
import os
import sys

# --- Setup & imports ---

try:
    from gltflib import (
        GLTF, BufferView, Accessor, BufferTarget, Animation, Channel, 
        AnimationSampler, Target, ComponentType, Attributes
    )
except ImportError:
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("Error", "Library 'gltflib' not found.\nPlease run: pip install gltflib")
    sys.exit(1)

# --- Core Logic ---

def process_gltf(input_file, output_file, node_name, speed, x_dir, y_dir):
    try:
        gltf = GLTF.load(input_file)
        
        # Validation
        if speed == 0:
            return False, "Speed cannot be zero."

        duration = 1.0 / abs(speed)
        dir_x = x_dir * (1 if speed > 0 else -1)
        dir_y = y_dir * (1 if speed > 0 else -1)

        # Find Node
        target_node_index = -1
        for i, node in enumerate(gltf.model.nodes):
            if node.name and node.name.strip().lower() == node_name.strip().lower():
                target_node_index = i
                break
        
        if target_node_index == -1:
            return False, f"Node '{node_name}' not found."

        target_node = gltf.model.nodes[target_node_index]
        if target_node.mesh is None:
            return False, "Selected node has no mesh."

        target_node.weights = [0.0, 0.0]

        # Prepare Morph Targets
        mesh = gltf.model.meshes[target_node.mesh]
        primitive = mesh.primitives[0]
        
        uv_accessor = gltf.model.accessors[primitive.attributes.TEXCOORD_0]
        count = uv_accessor.count
        buffer_idx = 0 

        if not gltf.model.buffers:
            return False, "Model has no buffers."
        
        offset = gltf.model.buffers[buffer_idx].byteLength
        
        # Binary Injection
        blob = struct.pack("".join(['f' for _ in range(count*2+1)]),*[t%2 for t in range(count*2+1)])
        gltf.resources[buffer_idx].data += blob
        gltf.model.buffers[buffer_idx].byteLength += len(blob)

        # Buffer Views & Accessors
        new_views_start = len(gltf.model.bufferViews)
        gltf.model.bufferViews.extend([
            BufferView(buffer=0, byteOffset=offset + t*4, byteLength=4*count*2, target=BufferTarget.ARRAY_BUFFER.value) 
            for t in range(2)
        ])

        new_accessors_start = len(gltf.model.accessors)
        gltf.model.accessors.extend([
            Accessor(
                bufferView=new_views_start + t, 
                componentType=ComponentType.FLOAT.value, 
                count=count, 
                type='VEC2', 
                max=[0.0 + t%2, 1.0 - t%2], 
                min=[0.0 + t%2, 1.0 - t%2]
            ) for t in range(2)
        ])

        primitive.targets = [Attributes(TEXCOORD_0=new_accessors_start + t) for t in range(2)]
        mesh.weights = [0.0, 0.0]

        # Animation Data
        anim_times = [0.0, duration]
        anim_weights = [0.0, 0.0, dir_y, dir_x]

        offset_time = gltf.model.buffers[buffer_idx].byteLength
        time_blob = struct.pack(f"{len(anim_times)}f", *anim_times)
        gltf.resources[buffer_idx].data += time_blob
        gltf.model.buffers[buffer_idx].byteLength += len(time_blob)

        offset_weights = gltf.model.buffers[buffer_idx].byteLength
        weights_blob = struct.pack(f"{len(anim_weights)}f", *anim_weights)
        gltf.resources[buffer_idx].data += weights_blob
        gltf.model.buffers[buffer_idx].byteLength += len(weights_blob)

        bv_time_idx = len(gltf.model.bufferViews)
        gltf.model.bufferViews.append(BufferView(buffer=0, byteOffset=offset_time, byteLength=len(time_blob)))
        
        bv_weights_idx = len(gltf.model.bufferViews)
        gltf.model.bufferViews.append(BufferView(buffer=0, byteOffset=offset_weights, byteLength=len(weights_blob)))

        acc_time_idx = len(gltf.model.accessors)
        gltf.model.accessors.append(Accessor(
            bufferView=bv_time_idx, componentType=ComponentType.FLOAT.value, count=len(anim_times), 
            type='SCALAR', max=[max(anim_times)], min=[0]
        ))

        acc_weights_idx = len(gltf.model.accessors)
        gltf.model.accessors.append(Accessor(
            bufferView=bv_weights_idx, componentType=ComponentType.FLOAT.value, count=len(anim_weights), 
            type='SCALAR', max=[max(anim_weights)], min=[min(anim_weights)]
        ))

        if gltf.model.animations is None:
            gltf.model.animations = []
        
        gltf.model.animations.append(
            Animation(
                name='UV_Speed_Anim',
                channels=[Channel(sampler=0, target=Target(node=target_node_index, path='weights'))],
                samplers=[AnimationSampler(input=acc_time_idx, output=acc_weights_idx, interpolation='LINEAR')]
            )
        )

        # Export using the specific output path provided by user
        gltf.export(output_file)
        return True, "Done"

    except Exception as e:
        return False, str(e)

# --- GUI Functions ---

def select_input_file():
    filename = filedialog.askopenfilename(filetypes=[("GLB Files", "*.glb")])
    if filename:
        filename = os.path.normpath(filename)
        entry_input.delete(0, tk.END)
        entry_input.insert(0, filename)
        
        # Auto-fill output path
        directory = os.path.dirname(filename)
        basename = os.path.basename(filename)
        name_part, ext_part = os.path.splitext(basename)
        # Suggests: out_original_name.glb in same folder
        suggested_output = os.path.join(directory, f"out_{name_part}{ext_part}")
        
        entry_output.delete(0, tk.END)
        entry_output.insert(0, suggested_output)
        
        load_nodes(filename)

def select_output_file():
    # Start browsing from the input file directory
    current_input = entry_input.get()
    initial_dir = os.path.dirname(current_input) if current_input else "/"
    
    filename = filedialog.asksaveasfilename(
        initialdir=initial_dir,
        defaultextension=".glb",
        filetypes=[("GLB Files", "*.glb")]
    )
    if filename:
        filename = os.path.normpath(filename)
        entry_output.delete(0, tk.END)
        entry_output.insert(0, filename)

def load_nodes(filepath):
    try:
        gltf = GLTF.load(filepath)
        node_names = []
        for node in gltf.model.nodes:
            if node.name and node.mesh is not None:
                node_names.append(node.name)
        
        if not node_names:
            node_names = ["No Mesh Found"]
        
        combo_nodes['values'] = node_names
        combo_nodes.current(0)
    except Exception as e:
        messagebox.showerror("Error", f"Could not read GLB file:\n{e}")

def apply_animation():
    in_path = entry_input.get()
    out_path = entry_output.get()
    node = combo_nodes.get()
    
    # GUI Validations
    if not in_path or not os.path.exists(in_path):
        messagebox.showwarning("Warning", "Please select a valid Input file.")
        return

    if not out_path:
        messagebox.showwarning("Warning", "Please define an Output file.")
        return

    if not node or node == "No Mesh Found":
        messagebox.showwarning("Warning", "Please select a valid Target Mesh.")
        return

    try:
        sp = float(entry_speed.get())
        x = float(entry_x.get())
        y = float(entry_y.get())
    except ValueError:
        messagebox.showerror("Error", "Speed, X, and Y must be numbers.")
        return

    # Call Logic
    success, result = process_gltf(in_path, out_path, node, sp, x, y)
    
    if success:
        messagebox.showinfo("Success!", f"File saved successfully at:\n{out_path}")
    else:
        messagebox.showerror("Processing Error", result)

# --- GUI Layout (No shortcut styling to avoid crashes) ---

root = tk.Tk()
root.title("GLB UV Animator Tool")
root.geometry("450x480") # Increased height for new fields
root.resizable(False, False)

# Input
tk.Label(root, text="1. Input File (.GLB):", font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=15, pady=(15, 5))
frame_input = tk.Frame(root)
frame_input.pack(fill='x', padx=15, pady=0)

entry_input = tk.Entry(frame_input)
entry_input.pack(side='left', fill='x', expand=True)
btn_browse_in = tk.Button(frame_input, text="Browse...", command=select_input_file)
btn_browse_in.pack(side='right', padx=(5, 0))

# Node
tk.Label(root, text="2. Target Mesh (Node):", font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=15, pady=(15, 5))
combo_nodes = ttk.Combobox(root, state="readonly")
combo_nodes.pack(fill='x', padx=15, pady=0)

# Settings
frame_settings = tk.LabelFrame(root, text=" 3. Animation Settings ", font=("Segoe UI", 9))
frame_settings.pack(fill='x', padx=15, pady=(15, 0))

tk.Label(frame_settings, text="Speed:").grid(row=0, column=0, sticky='e', padx=10, pady=10)
entry_speed = tk.Entry(frame_settings, width=10)
entry_speed.insert(0, "1.0")
entry_speed.grid(row=0, column=1, sticky='w', padx=0, pady=10)

tk.Label(frame_settings, text="X Direction (-1 to 1):").grid(row=1, column=0, sticky='e', padx=10, pady=5)
entry_x = tk.Entry(frame_settings, width=10)
entry_x.insert(0, "1.0")
entry_x.grid(row=1, column=1, sticky='w', padx=0, pady=5)

tk.Label(frame_settings, text="Y Direction (-1 to 1):").grid(row=2, column=0, sticky='e', padx=10, pady=10)
entry_y = tk.Entry(frame_settings, width=10)
entry_y.insert(0, "0.0")
entry_y.grid(row=2, column=1, sticky='w', padx=0, pady=10)

# Output
tk.Label(root, text="4. Output File:", font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=15, pady=(15, 5))
frame_output = tk.Frame(root)
frame_output.pack(fill='x', padx=15, pady=0)

entry_output = tk.Entry(frame_output)
entry_output.pack(side='left', fill='x', expand=True)
btn_browse_out = tk.Button(frame_output, text="Save As...", command=select_output_file)
btn_browse_out.pack(side='right', padx=(5, 0))

# Apply
btn_apply = tk.Button(root, text="Apply UV Anim", command=apply_animation, 
                      bg="#2e7d32", fg="white", font=("Segoe UI", 10, "bold"), height=2)
btn_apply.pack(fill='x', padx=20, pady=25)

# (Copyright)
footer_text = "Rakíel © 2026"
lbl_footer = tk.Label(root, text=footer_text, fg="gray", font=("Arial", 8))
lbl_footer.pack(side="bottom", pady=10)

root.mainloop()