# GLB UV Animator Tool
This is a tool for adding UV animations to GLB models used in [Ikemen GO](https://github.com/ikemen-engine/Ikemen-GO).

# Run from source

With Python and Tkinter installed, run:

```sh
python -m pip install -r requirements.txt
python GLB-UV-Animator.py
```

# Usage:
1. Open a GLB model. Its nodes will be loaded automatically.
2. Find the node where you want to apply the animation.
3. Set the animation speed and direction.
4. Choose an output file (optional). The default is `out_<original name>.glb` in the same directory as the input.
5. Click "Add to Queue" to add your changes to the animation queue on the right.
6. Click "Apply Animations".

# Notes:
- The UV Preview shows the selected mesh with its GLB node orientation. Left-drag to orbit, right-drag to pan, and use the mouse wheel or the zoom buttons to zoom. **Reset View** restores the camera, and **Show Saved** returns to the animation in the file after editing Speed/X/Y.
- Speed starts at zero. Existing UV animations play when their node is selected. Enter a nonzero speed to preview and queue new settings.
- The preview uses OpenGL 3.3 when available and falls back to CPU rendering. It displays embedded base-color textures on triangle meshes with simplified materials.
- Each node can have one queued UV animation. Opening a GLB resets the queue.
- Every primitive of a selected mesh needs `TEXCOORD_0`. Nodes with animated morph weights are marked in the list. Applying the queue replaces compatible UV scrolling clips and reports a conflict for other weight animations.
- These animations are generally meant to be applied at the final stage of stage creation.
- If you open the GLB file in Blender, you won’t be able to see the animations.
