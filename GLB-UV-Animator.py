import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# --- Setup & imports ---

try:
    from uv_export import GLTF, UVAnimation, export_glb, has_weight_animation
except ModuleNotFoundError as exc:
    if exc.name != "gltflib":
        raise
    GLTF = None


def create_dark_theme(style):
    background = "#202124"
    field = "#292b30"
    control = "#353840"
    border = "#555a66"
    text = "#f0f1f4"
    accent = "#385a94"

    style.theme_create("animator-dark", parent="clam", settings={
        ".": {"configure": {
            "background": background, "foreground": text, "fieldbackground": field,
            "bordercolor": border, "lightcolor": control, "darkcolor": control,
            "arrowcolor": text, "selectbackground": accent, "selectforeground": text,
            "insertcolor": text,
        }},
        "TButton": {
            "configure": {"background": control, "focuscolor": text, "anchor": "center"},
            "map": {"background": [("pressed", accent), ("active", border)]},
        },
        "Vertical.TScrollbar": {
            "configure": {"background": control, "troughcolor": field},
            "map": {"background": [("active", border)]},
        },
        "TEntry": {"map": {
            "fieldbackground": [("readonly", field)], "bordercolor": [("focus", accent)],
        }},
        "TCombobox": {"map": {
            "fieldbackground": [("readonly", field)], "bordercolor": [("focus", accent)],
            "background": [("active", border), ("readonly", control)],
        }},
    })


class AnimatorApp:
    # --- GUI Layout ---

    def __init__(self, root):
        self.root = root
        self.source = None
        self.source_path = None
        self.node_indices = []
        self.queue = {}
        self.style = ttk.Style(root)
        self.light_theme = self.style.theme_use()
        self.style.configure("TButton", anchor="center")
        create_dark_theme(self.style)
        self.input_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.speed = tk.StringVar(value="1.0")
        self.x = tk.StringVar(value="1.0")
        self.y = tk.StringVar(value="0.0")

        root.title("GLB UV Animator Tool")
        screen_width, screen_height = root.winfo_screenwidth(), root.winfo_screenheight()
        width, height = min(760, screen_width), min(440, screen_height)
        left, top = (screen_width - width) // 2, (screen_height - height) // 2
        root.geometry(f"{width}x{height}+{left}+{top}")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)

        # Containers
        content = ttk.Frame(root, padding=15)
        content.grid(sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=1)
        content.rowconfigure(0, weight=1)

        settings = ttk.Frame(content)
        settings.grid(row=0, column=0, sticky="new", padx=(0, 15))
        settings.columnconfigure(0, weight=1)
        # Input
        self.path_field(settings, 0, "Input File (.GLB)", self.input_path, "Browse...", self.open_model, True)

        # Node
        ttk.Label(settings, text="Target Mesh (Node)").grid(row=1, sticky="w", pady=(12, 4))
        self.nodes = ttk.Combobox(settings, state="readonly", width=30)
        self.nodes.grid(row=2, sticky="ew")

        # Settings
        fields = ttk.LabelFrame(settings, text="Animation Settings", padding=8)
        fields.grid(row=3, sticky="ew", pady=12)
        for row, (label, variable) in enumerate((
            ("Speed", self.speed),
            ("X Direction (-1 to 1)", self.x),
            ("Y Direction (-1 to 1)", self.y),
        )):
            ttk.Label(fields, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
            ttk.Entry(fields, width=10, textvariable=variable).grid(row=row, column=1, pady=3)

        # Output
        self.path_field(settings, 4, "Output File", self.output_path, "Save As...", self.choose_output)

        # Operations Container
        queue_panel = ttk.LabelFrame(content, text="Animation Queue", padding=8)
        queue_panel.grid(row=0, column=1, sticky="nsew")
        queue_panel.columnconfigure((0, 1), weight=1, uniform="queue-actions")
        queue_panel.rowconfigure(0, weight=1)
        self.queue_list = tk.Listbox(queue_panel, width=37, height=12)
        self.queue_list.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self.queue_scrollbar = ttk.Scrollbar(queue_panel, command=self.queue_list.yview)
        self.queue_list.configure(yscrollcommand=self.update_queue_scrollbar)
        # Add Animation button
        ttk.Button(queue_panel, text="Add to Queue", command=self.add_animation).grid(
            row=1, column=0, sticky="ew", padx=(0, 4), pady=(8, 0),
        )
        # Remove Animation button
        ttk.Button(queue_panel, text="Remove", command=self.remove_animation).grid(
            row=1, column=1, sticky="ew", padx=(4, 0), pady=(8, 0),
        )

        # Apply button
        ttk.Button(content, text="Apply UV Animations", command=self.apply_animations).grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(15, 8),
        )
        # (Copyright)
        ttk.Label(content, text="Rakíel © 2026").grid(row=2, column=0, sticky="w")
        appearance = ttk.Frame(content)
        appearance.grid(row=2, column=1, sticky="e")
        ttk.Label(appearance, text="Theme").pack(side="left", padx=(0, 6))
        self.theme = ttk.Combobox(appearance, values=("Light", "Dark"), width=6, state="readonly")
        self.theme.current(0)
        self.theme.pack(side="left")
        self.theme.bind("<<ComboboxSelected>>", self.change_theme)
        self.change_theme()

    # --- GUI Functions ---

    def update_queue_scrollbar(self, first, last):
        self.queue_scrollbar.set(first, last)
        if float(first) == 0 and float(last) == 1:
            self.queue_scrollbar.grid_remove()
        else:
            self.queue_scrollbar.grid(row=0, column=2, sticky="ns")

    def change_theme(self, event=None):
        self.style.theme_use("animator-dark" if self.theme.get() == "Dark" else self.light_theme)
        background = self.style.lookup("TFrame", "background")
        self.root.configure(background=background)
        colors = {
            "background": self.style.lookup("TEntry", "fieldbackground") or self.root.cget("background"),
            "foreground": self.style.lookup(".", "foreground"),
            "selectbackground": self.style.lookup(".", "selectbackground"),
            "selectforeground": self.style.lookup(".", "selectforeground"),
            "highlightbackground": background,
        }
        self.queue_list.configure(**{key: value for key, value in colors.items() if value})

    def path_field(self, parent, row, label, variable, button, command, readonly=False):
        frame = ttk.Frame(parent)
        frame.grid(row=row, sticky="ew")
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text=label).grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Entry(frame, textvariable=variable, state="readonly" if readonly else "normal").grid(
            row=1, column=0, sticky="ew",
        )
        ttk.Button(frame, text=button, command=command).grid(row=1, column=1, padx=(5, 0))

    def open_model(self):
        filename = filedialog.askopenfilename(parent=self.root, filetypes=[("GLB Files", "*.glb")])
        if not filename:
            return
        path = Path(filename)
        try:
            source = GLTF.load_glb(str(path))
        except Exception as exc:
            messagebox.showerror("Error", f"Could not read GLB file:\n{exc}", parent=self.root)
            return

        self.source = source
        self.source_path = path
        self.input_path.set(str(path))
        # Auto-fill output path
        # Suggests: out_original_name.glb in same folder
        self.output_path.set(str(path.with_name(f"out_{path.name}")))
        self.queue.clear()
        self.refresh_queue()

        self.node_indices = [
            index for index, node in enumerate(source.model.nodes or []) if node.mesh is not None
        ]
        self.nodes["values"] = [
            self.node_label(index) + (" (animated weights)" if self.has_weight_animation(index) else "")
            for index in self.node_indices
        ]
        if self.node_indices:
            self.nodes.current(0)
        else:
            self.nodes.set("")
            messagebox.showwarning("No Mesh", "This GLB has no nodes with meshes.", parent=self.root)

    def node_label(self, index):
        node = self.source.model.nodes[index]
        return f"{node.name or 'Unnamed node'} [#{index}]"

    def has_weight_animation(self, index):
        model = self.source.model
        return has_weight_animation(model.animations or [], index, model.nodes[index].mesh)

    def choose_output(self):
        # Start browsing from the input file directory
        filename = filedialog.asksaveasfilename(
            parent=self.root,
            initialdir=self.source_path.parent if self.source_path else Path.home(),
            defaultextension=".glb",
            filetypes=[("GLB Files", "*.glb")],
        )
        if filename:
            self.output_path.set(filename)

    def refresh_queue(self):
        # Update GUI queue
        self.queue_list.delete(0, tk.END)
        for entry in self.queue.values():
            self.queue_list.insert(
                tk.END, f"{self.node_label(entry.node_index)}  |  {entry.speed:g} / {entry.x:g}, {entry.y:g}",
            )

    def add_animation(self):
        selected = self.nodes.current()
        if selected < 0:
            messagebox.showwarning("Target Mesh", "Open a GLB and select a mesh.", parent=self.root)
            return
        index = self.node_indices[selected]
        try:
            speed, x, y = (float(field.get()) for field in (self.speed, self.x, self.y))
        except ValueError:
            messagebox.showerror("Error", "Speed, X, and Y must be numbers.", parent=self.root)
            return

        entry = UVAnimation(index, speed, x, y)
        try:
            entry.keyframes()
        except ValueError as exc:
            messagebox.showerror("Error", str(exc), parent=self.root)
            return
        if index in self.queue:
            messagebox.showwarning(
                "Animation Queue", "This node is already queued. Remove its entry to change the settings.",
                parent=self.root,
            )
            return
        self.queue[index] = entry
        self.refresh_queue()

    def remove_animation(self):
        selected = self.queue_list.curselection()
        if not selected:
            messagebox.showwarning("Animation Queue", "Select an animation to remove.", parent=self.root)
            return
        index = tuple(self.queue)[selected[0]]
        del self.queue[index]
        self.refresh_queue()

    def apply_animations(self):
        if self.source is None or not self.queue:
            messagebox.showwarning("Animation Queue", "Open a GLB and queue an animation.", parent=self.root)
            return
        filename = self.output_path.get().strip()
        if not filename:
            messagebox.showwarning("Output File", "Choose an output file.", parent=self.root)
            return

        try:
            export_glb(self.source, filename, self.queue.values())
        except Exception as exc:
            messagebox.showerror("Processing Error", str(exc), parent=self.root)
        else:
            messagebox.showinfo("Success", f"File saved at:\n{filename}", parent=self.root)


def main():
    root = tk.Tk()
    if GLTF is None:
        root.withdraw()
        messagebox.showerror(
            "Error", "Library 'gltflib' not found.\nPlease run: python -m pip install -r requirements.txt",
            parent=root,
        )
        root.destroy()
        return 1
    AnimatorApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
