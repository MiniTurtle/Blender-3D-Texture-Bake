import bpy
import os
import numpy as np
import tempfile

bl_info = {
    "name": "Texture 3D Bake",
    "blender": (4, 3, 2),
    "category": "Render",
}

def dump(obj):
   for attr in dir(obj):
       if hasattr( obj, attr ):
           print( "obj.%s = %s" % (attr, getattr(obj, attr)))

def setup_compositing_from_bake_type():
    scene = bpy.context.scene
    bake_type = scene.cycles.bake_type.upper()  # normalize the value

    # Ensure we use nodes and get the node tree.
    scene.use_nodes = True
    tree = scene.node_tree

    # Reset (clear) all existing nodes.
    tree.nodes.clear()

    # Create Render Layers, Composite, and Viewer nodes.
    render_node = tree.nodes.new(type="CompositorNodeRLayers")
    composite_node = tree.nodes.new(type="CompositorNodeComposite")
    viewer_node = tree.nodes.new(type="CompositorNodeViewer")

    # Position nodes (optional, for better layout).
    render_node.location = (-300, 0)
    composite_node.location = (200, 0)
    viewer_node.location = (200, -200)

    # Reset view layer passes.
    view_layer = bpy.context.view_layer
    view_layer.use_pass_diffuse_color = False
    view_layer.use_pass_normal = False

    links = tree.links

    # Based on bake type, enable the correct pass and choose the corresponding output socket.
    if bake_type == 'DIFFUSE':
        view_layer.use_pass_diffuse_color = True
        # The Render Layers node outputs Diffuse Color as "DiffCol"
        output_socket = render_node.outputs.get('DiffCol')
        if output_socket is None:
            print("Warning: 'DiffCol' output not found. Falling back to Image output.")
            output_socket = render_node.outputs.get('Image')
            
        output_alpha_socket = render_node.outputs.get('Alpha')
        links.new(output_alpha_socket, composite_node.inputs[1])
        links.new(output_alpha_socket, viewer_node.inputs[1])
    
    elif bake_type == 'NORMAL':
        view_layer.use_pass_normal = True

        normal_socket = render_node.outputs.get('Normal')
        if normal_socket is None:
            normal_socket = render_node.outputs.get('Image')
        
        # --- Multiply Node ---
        # Multiplies the normal data by (0.5, 0.5, 0.5).
        multiply_node = tree.nodes.new(type="CompositorNodeMixRGB")
        multiply_node.blend_type = 'MULTIPLY'
        multiply_node.use_clamp = True
        multiply_node.inputs[0].default_value = 1.0  # Factor (unused for MULTIPLY)
        # Set Color2 to (0.5, 0.5, 0.5, 1)
        multiply_node.inputs[2].default_value = (0.5, 0.5, 0.5, 1.0)
        multiply_node.location = (-300, 0)
        # Link Normal output to Color1 of Multiply node.
        links.new(normal_socket, multiply_node.inputs[1])
        
        # --- Add Node ---
        # Adds (0.5, 0.5, 0.5) to the multiplied result.
        add_node = tree.nodes.new(type="CompositorNodeMixRGB")
        add_node.blend_type = 'ADD'
        add_node.use_clamp = True
        add_node.inputs[0].default_value = 1.0  # Factor
        add_node.inputs[2].default_value = (0.5, 0.5, 0.5, 1.0)
        add_node.location = (-100, 0)
        links.new(multiply_node.outputs[0], add_node.inputs[1])
        
        # --- Invert Node ---
        # Inverts the colors of the add node output.
        invert_node = tree.nodes.new(type="CompositorNodeInvert")
        invert_node.location = (100, 0)
        links.new(add_node.outputs[0], invert_node.inputs[1])
        
        # --- Constant RGB Node ---
        # Provides the constant color (0.5, 0.5, 1.0) for the Mix node.
        rgb_node = tree.nodes.new(type="CompositorNodeRGB")
        rgb_node.outputs[0].default_value = (0.5, 0.5, 1.0, 1.0)
        rgb_node.location = (100, -200)
        
        # --- Mix Node ---
        # Mixes the inverted normal with the constant color using the Render Layer Alpha as the factor.
        mix_node = tree.nodes.new(type="CompositorNodeMixRGB")
        mix_node.blend_type = 'MIX'
        mix_node.location = (300, 0)
        # Connect Render Layer Alpha to the Factor input.
        alpha_socket = render_node.outputs.get('Alpha')
        if alpha_socket is None:
            # If Alpha is missing, default to factor 1.
            mix_node.inputs[0].default_value = 1.0
        else:
            links.new(alpha_socket, mix_node.inputs[0])
        # Set Image A to constant (0.5,0.5,1.0) and Image B from the invert node.
        links.new(rgb_node.outputs[0], mix_node.inputs[1])
        links.new(invert_node.outputs[0], mix_node.inputs[2])
        
        output_socket = mix_node.outputs.get('Image')
        if output_socket is None:
            print("Warning: 'Mix node Image' output not found. Falling back to Image output.")
            output_socket = render_node.outputs.get('Image')
    else:
        # Fallback: use the regular image output.
        output_socket = render_node.outputs.get('Image')

    # Hook up the selected output to Composite and Viewer nodes.
  
    links.new(output_socket, composite_node.inputs[0])
    links.new(output_socket, viewer_node.inputs[0])


def render_slices(self, context, texture_size, output_folder):
    obj = None
    
    if len(context.selected_objects) > 0:
        obj = context.selected_objects[0]
        
    if obj is None or obj.type != 'MESH':
        self.report({'ERROR'}, "No valid object selected.")
        return
    
    depsgraph = context.evaluated_depsgraph_get()
    eval_obj = obj.evaluated_get(depsgraph)
    mesh = eval_obj.to_mesh()
    
    if mesh is None or len(mesh.vertices) <= 0:
        self.report({'ERROR'}, "Selected object is not a realized mesh")
        return
    
    # Get all vertex positions in world space
    vertices = [eval_obj.matrix_world @ v.co for v in mesh.vertices]

    
    # Compute bounding box
    min_coords = np.min(vertices, axis=0)
    max_coords = np.max(vertices, axis=0)
    
    # Get object bounds
    min_x, min_y, min_z = min_coords
    max_x, max_y, max_z = max_coords
    
    # Compute slice depth values
    num_slices = texture_size[2]
    slice_depths = np.linspace(min_z, max_z, num_slices)
    
    # Create an orthographic camera
    cam_data = bpy.data.cameras.new(name="Slice_Camera")
    cam_data.type = 'ORTHO'
    cam_obj = bpy.data.objects.new("Slice_Camera", cam_data)
    bpy.context.scene.collection.objects.link(cam_obj)
    
    loc_z_offset = 1.0
    cam_obj.location = (0, 0, max_z + loc_z_offset)
    cam_obj.rotation_euler = (0, 0, 0)  # Pointing down -Z axis
    
    cam_data.ortho_scale = max(max_x - min_x, max_y - min_y)
    bpy.context.scene.camera = cam_obj
    
    # Set render settings
    scene = context.scene
    scene.render.engine = 'CYCLES'
    scene.render.resolution_x = texture_size[0]
    scene.render.resolution_y = texture_size[1]    
    setup_compositing_from_bake_type()
    
    img_rows = 999
    img_cols = 999
    for d in range(int(texture_size[2]), 0, -1):
        f = float(texture_size[2]) / d
        if not f.is_integer():
            continue
        
        i = int(f)
        
        if i*i+d*d > img_rows*img_rows + img_cols*img_cols:
            continue
 
        img_rows = d
        img_cols = i
        
    img_width = int(texture_size[0] * img_cols)
    img_height = int(texture_size[1] * img_rows)
        
    scene.texture_3d_progress = 0.0
    wm = bpy.context.window_manager
    wm.progress_begin(0, len(slice_depths))
    
    # Render each slice
    image_slices = []
    for i, depth in enumerate(slice_depths):
        scene.texture_3d_progress = i / len(slice_depths)
        wm.progress_update(i)
        
        cam_data.clip_start = loc_z_offset + depth - 0.01
        cam_data.clip_end = loc_z_offset + depth + 0.1
        
        filename = os.path.join(output_folder, f"slice_{i:03d}.png")
        bpy.context.scene.render.filepath = filename
        bpy.ops.render.render(write_still=True)
        
        image_slices.append(filename)
        
    wm.progress_end()

    
    # Create a 3D texture using Blender’s Image system
    try:
       
        
        img_3d = bpy.data.images.get("3D_Texture") 
        if img_3d:
            img_3d.scale(img_width, img_height)
        else:
            img_3d = bpy.data.images.new("3D_Texture", 
                width=img_width, 
                height=img_height)
            img_3d.file_format = 'PNG'
            
        #pixels_size = texture_size[0] * texture_size[1] * 4
        #img_3d.pixels = [0.0] * (pixels_size * texture_size[2])
        
        slice_width = texture_size[0]  # Width of one slice
        slice_height = texture_size[1]  # Height of one slice

        for i, img_path in enumerate(image_slices):
            slice_img = bpy.data.images.load(img_path)
            pixels = list(slice_img.pixels)
            if len(pixels) != slice_width*slice_height*4:
                print("pixels different size")
                
            # Calculate where to paste the slice in the sprite sheet
            col = i % img_cols  # Column index in the sprite sheet
            row = i // img_cols  # Row index in the sprite sheet (starting from top)
            
            print(int(i / len(image_slices)), "%")

            # Paste row by row
            for y in range(slice_height):
                slice_start = y * slice_width
                slice_end = slice_start + slice_width

                sprite_start = ((img_rows - 1 - row) * slice_height + y) * img_width + col * slice_width
                sprite_end = sprite_start + slice_width

                img_3d.pixels[int(sprite_start*4):int(sprite_end*4)] = pixels[int(slice_start*4):int(slice_end*4)]
                
            #img_3d.pixels[i * pixels_size : (i+1) * pixels_size] = pixels
            bpy.data.images.remove(slice_img)
        
        #img_3d.filepath_raw = os.path.join(output_folder, "3d_texture.png")
        #img_3d.save()
        print("3D Texture saved successfully.")
        print("Cols: ", img_cols, "Rows: ", img_rows, "Width: ", img_width, "Height: ", img_height)
    
    except Exception as e:
        print("Failed to create 3D texture:", e)
        
    with bpy.context.temp_override(selected_objects=[cam_obj]):
        bpy.ops.object.delete()
        

# Define properties in the Scene
def register_properties():
    bpy.types.Scene.texture_size_x = bpy.props.IntProperty(name="Size X", default=256, min=1)
    bpy.types.Scene.texture_size_y = bpy.props.IntProperty(name="Size Y", default=256, min=1)
    bpy.types.Scene.texture_size_z = bpy.props.IntProperty(name="Size Z", default=256, min=1)
    bpy.types.Scene.texture_3d_progress = bpy.props.FloatProperty()


def unregister_properties():
    del bpy.types.Scene.texture_size_x
    del bpy.types.Scene.texture_size_y
    del bpy.types.Scene.texture_size_z
    

class BakeTexture3DOperator(bpy.types.Operator):
    """Tooltip"""
    bl_idname = "render.bake_3d_texture_operator"
    bl_label = "Bake 3D Texture"

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def execute(self, context):
        scene = context.scene
        texture_size = [scene.texture_size_x, scene.texture_size_y, scene.texture_size_z]

        with tempfile.TemporaryDirectory() as tmpdirname:
            render_slices(self, context, texture_size, tmpdirname)
        
        return {'FINISHED'}


#def menu_func(self, context):
    #self.layout.operator(BakeTexture3DOperator.bl_idname, text=BakeTexture3DOperator.bl_label)

class LayoutPanel(bpy.types.Panel):
    """Creates a Panel in the scene context of the properties editor"""
    bl_label = "Texture 3D"
    bl_idname = "RENDER_PT_texture_3d"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "render"
        

    def draw(self, context):
        layout = self.layout

        scene = context.scene
        
        #layout.progress(factor = scene.texture_3d_progress, type = 'BAR')
        
        # Big render button
        row = layout.row()
        row.scale_y = 1.0
        row.operator("render.bake_3d_texture_operator")
        
        layout.prop(context.scene.render, "film_transparent")
        layout.prop(context.scene.cycles, "bake_type")

        # Create texture size.
        layout.label(text=" Texture Size:")

        row = layout.row()
        
        row.prop(scene, "texture_size_x")
        row.prop(scene, "texture_size_y")
        row.prop(scene, "texture_size_z")


def register():
    register_properties()
    bpy.utils.register_class(BakeTexture3DOperator)
    #bpy.types.VIEW3D_MT_object.append(menu_func)
    bpy.utils.register_class(LayoutPanel)


def unregister():
    unregister_properties()
    bpy.utils.unregister_class(BakeTexture3DOperator)
    #bpy.types.VIEW3D_MT_object.remove(menu_func)
    bpy.utils.unregister_class(LayoutPanel)


if __name__ == "__main__":
    register()
