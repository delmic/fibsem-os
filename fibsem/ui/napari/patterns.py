import logging
import time
from copy import deepcopy
from typing import List, Optional, Tuple, Union

import matplotlib.pyplot as plt
import napari
import numpy as np
from napari.layers import Image as NapariImageLayer
from napari.layers import Layer as NapariLayer
from napari.layers import Shapes as NapariShapesLayers
from PIL import Image

from fibsem.milling import FibsemMillingStage
from fibsem.milling.patterning.patterns2 import (
    BasePattern,
    FiducialPattern,
)
from fibsem.milling.patterning.plotting import compose_pattern_image
from fibsem.structures import (
    FibsemBitmapSettings,
    FibsemCircleSettings,
    FibsemImage,
    FibsemLineSettings,
    FibsemPatternSettings,
    FibsemRectangle,
    FibsemRectangleSettings,
    Point,
    calculate_fiducial_area_v2,
)
from fibsem.milling.patterning.utils import create_pattern_mask

# colour wheel
COLOURS = ["yellow", "cyan", "magenta", "lime", "orange", "hotpink", "green", "blue", "red", "purple"]

SHAPES_LAYER_PROPERTIES = {
    "edge_width": 0.5, 
    "opacity": 0.5, 
    "blending": "translucent",
    "line_edge_width": 3
}
IMAGE_LAYER_PROPERTIES = {
    "blending": "additive",
    "opacity": 0.6,
    "cmap": {0: "black", 1: COLOURS[0]} # override with colour wheel
}


IGNORE_SHAPES_LAYERS = ["ruler_line", "crosshair", "scalebar", "label", "alignment_area"] # ignore these layers when removing all shapes
IMAGE_PATTERN_LAYERS = ["annulus-layer", "bmp_Image"]
STAGE_POSTIION_SHAPE_LAYERS = ["saved-stage-positions", "current-stage-position"] # for minimap
IGNORE_SHAPES_LAYERS.extend(STAGE_POSTIION_SHAPE_LAYERS)

def get_image_pixel_centre(shape: Tuple[int, int]) -> Tuple[int, int]:
    """Get the centre of the image in pixel coordinates."""
    icy, icx = shape[0] // 2, shape[1] // 2
    return icy, icx

def convert_pattern_to_napari_circle(
    pattern_settings: FibsemCircleSettings, shape: Tuple[int, int], pixelsize: float
):
    if not isinstance(pattern_settings, FibsemCircleSettings):
        raise ValueError(f"Pattern is not a Circle: {pattern_settings}")
    
    # image centre
    icy, icx = get_image_pixel_centre(shape)
    
    # pattern to pixel coords
    r = int(pattern_settings.radius / pixelsize)
    cx = int(icx + (pattern_settings.centre_x / pixelsize))
    cy = int(icy - (pattern_settings.centre_y / pixelsize))

    # create corner coords
    xmin, ymin = cx - r, cy - r
    xmax, ymax = cx + r, cy + r

    # create circle
    shape = [[ymin, xmin], [ymin, xmax], [ymax, xmax], [ymax, xmin]]  # ??
    return np.array(shape)


def convert_pattern_to_napari_line(
    pattern_settings: FibsemLineSettings, 
    shape: Tuple[int, int], 
    pixelsize: float
) -> np.ndarray:
    
    if not isinstance(pattern_settings, FibsemLineSettings):
        raise ValueError(f"Pattern is not a Line: {pattern_settings}")
    
    # image centre
    icy, icx = get_image_pixel_centre(shape)
     
    # extract pattern information from settings
    start_x = pattern_settings.start_x
    start_y = pattern_settings.start_y
    end_x = pattern_settings.end_x
    end_y = pattern_settings.end_y

    # pattern to pixel coords
    px0 = int(icx + (start_x / pixelsize))
    py0 = int(icy - (start_y / pixelsize))
    px1 = int(icx + (end_x / pixelsize))
    py1 = int(icy - (end_y / pixelsize))

    # napari shape format [[y_start, x_start], [y_end, x_end]])
    shape = [[py0, px0], [py1, px1]]
    return np.array(shape)

def convert_pattern_to_napari_rect(
    pattern_settings: FibsemRectangleSettings, shape: Tuple[int, int], pixelsize: float
) -> np.ndarray:
    
    if not isinstance(pattern_settings, FibsemRectangleSettings):
        raise ValueError(f"Pattern is not a Rectangle: {pattern_settings}")

    # image centre
    icy, icx = get_image_pixel_centre(shape)
    
    # extract pattern information from settings
    pattern_width = pattern_settings.width
    pattern_height = pattern_settings.height
    pattern_centre_x = pattern_settings.centre_x
    pattern_centre_y = pattern_settings.centre_y
    pattern_rotation = pattern_settings.rotation

    # pattern to pixel coords
    w = int(pattern_width / pixelsize)
    h = int(pattern_height / pixelsize)
    cx = int(icx + (pattern_centre_x / pixelsize))
    cy = int(icy - (pattern_centre_y / pixelsize))
    r = -pattern_rotation  #
    xmin, xmax = -w / 2, w / 2
    ymin, ymax = -h / 2, h / 2
    px0 = cx + (xmin * np.cos(r) - ymin * np.sin(r))
    py0 = cy + (xmin * np.sin(r) + ymin * np.cos(r))
    px1 = cx + (xmax * np.cos(r) - ymin * np.sin(r))
    py1 = cy + (xmax * np.sin(r) + ymin * np.cos(r))
    px2 = cx + (xmax * np.cos(r) - ymax * np.sin(r))
    py2 = cy + (xmax * np.sin(r) + ymax * np.cos(r))
    px3 = cx + (xmin * np.cos(r) - ymax * np.sin(r))
    py3 = cy + (xmin * np.sin(r) + ymax * np.cos(r))
    # napari shape format
    shape = [[py0, px0], [py1, px1], [py2, px2], [py3, px3]]
    return np.array(shape)

def create_crosshair_shape(centre_point: Point, 
                           shape: Tuple[int, int], 
                           pixelsize: float) -> np.ndarray:

    icy, icx = shape[0] // 2, shape[1] // 2

    pattern_centre_x = centre_point.x
    pattern_centre_y = centre_point.y

    cx = int(icx + (pattern_centre_x / pixelsize))
    cy = int(icy - (pattern_centre_y / pixelsize))

    r_angles = [0,np.deg2rad(90)] #
    w = 40
    h = 1
    crosshair_shapes = []

    for r in r_angles:
        xmin, xmax = -w / 2, w / 2
        ymin, ymax = -h / 2, h / 2
        px0 = cx + (xmin * np.cos(r) - ymin * np.sin(r))
        py0 = cy + (xmin * np.sin(r) + ymin * np.cos(r))
        px1 = cx + (xmax * np.cos(r) - ymin * np.sin(r))
        py1 = cy + (xmax * np.sin(r) + ymin * np.cos(r))
        px2 = cx + (xmax * np.cos(r) - ymax * np.sin(r))
        py2 = cy + (xmax * np.sin(r) + ymax * np.cos(r))
        px3 = cx + (xmin * np.cos(r) - ymax * np.sin(r))
        py3 = cy + (xmin * np.sin(r) + ymax * np.cos(r))
        # napari shape format
        shape = [[py0, px0], [py1, px1], [py2, px2], [py3, px3]]
        crosshair_shapes.append(shape)

    return np.array(crosshair_shapes)


def convert_bitmap_pattern_to_napari_image(
        pattern_settings: FibsemBitmapSettings, shape: Tuple[int, int], pixelsize: float) -> np.ndarray:

    icy, icx = get_image_pixel_centre(shape)

    resize_x = int(pattern_settings.width / pixelsize)
    resize_y = int(pattern_settings.height / pixelsize)

    
    image_bmp = Image.open(pattern_settings.path)
    image_resized = image_bmp.resize((resize_x, resize_y))
    image_rotated = image_resized.rotate(-pattern_settings.rotation, expand=True)
    img_array = np.array(image_rotated)

    pattern_centre_x = int(icx - pattern_settings.width/pixelsize/2) # TODO: account for FIB translation 
    pattern_centre_y = int(icy - pattern_settings.height/pixelsize/2)

    pattern_point_x = int(pattern_centre_x + pattern_settings.centre_x / pixelsize)
    pattern_point_y = int(pattern_centre_y - pattern_settings.centre_y / pixelsize)

    translate_position = (pattern_point_y,pattern_point_x)

    
    return img_array, translate_position

def remove_all_napari_shapes_layers(viewer: napari.Viewer, layer_type: NapariLayer = NapariShapesLayers, ignore: List[str] = []):
    """Remove all shapes layers from the napari viewer, excluding a specified list."""
    # remove all shapes layers
    layers_to_remove = []
    layers_to_ignore = IGNORE_SHAPES_LAYERS + ignore
    for layer in viewer.layers:

        if layer.name in layers_to_ignore:
            continue
        if isinstance(layer, layer_type) or any([layer_name in layer.name for layer_name in IMAGE_PATTERN_LAYERS]):
            layers_to_remove.append(layer)
    for layer in layers_to_remove:
        viewer.layers.remove(layer)  # Not removing the second layer?


NAPARI_DRAWING_FUNCTIONS = {
    FibsemRectangleSettings: convert_pattern_to_napari_rect,
    FibsemCircleSettings: convert_pattern_to_napari_circle,
    FibsemLineSettings: convert_pattern_to_napari_line,
    FibsemBitmapSettings: convert_bitmap_pattern_to_napari_image,
}

NAPARI_PATTERN_LAYER_TYPES = {
    FibsemRectangleSettings: "rectangle",
    FibsemCircleSettings: "ellipse",
    FibsemLineSettings: "line",
    FibsemBitmapSettings: "image",
}

def draw_milling_patterns_in_napari(
    viewer: napari.Viewer,
    image_layer: NapariImageLayer,
    milling_stages: List[FibsemMillingStage],
    pixelsize: float,
    draw_crosshair: bool = True,
    background_milling_stages: Optional[List[FibsemMillingStage]] = None,
) -> List[str]:
    """Draw the milling patterns in napari as a combination of Shapes and Label layers.
    Args:
        viewer: napari viewer instance
        image: image to draw patterns on
        translation: translation of the FIB image layer
        milling_stages): list of milling stages
        draw_crosshair: draw crosshair on the image
        background_milling_stages: optional list of background milling stages to draw
    Returns:
        List[str]: list of milling pattern layers
    """

    # base image properties
    image_shape = image_layer.data.shape
    translation = image_layer.translate

    # draw milling patterns as labels
    # mask = np.zeros(image_shape, dtype=np.uint8)
    # colormap = {0: 'black'}
    # for i, stage in enumerate(deepcopy(milling_stages)):
    #     m = create_pattern_mask(stage, image_shape, pixelsize, include_exclusions=True)
    #     mask[m > 0] = i + 1
    #     colormap[i + 1] = COLOURS[i % len(COLOURS)]

    # name = "Milling Patterns"
    # # viewer = napari.Viewer()
    # if name in viewer.layers:
    #     viewer.layers[name].data = mask
    #     viewer.layers[name].colormap = colormap
    # else:
    #     viewer.add_labels(mask, name=name, colormap=colormap, translate=translation, blending='additive', opacity=0.9)

    # return [name]

    all_napari_shapes: List[np.ndarray] = []
    all_shape_types: List[np.ndarray] = []
    all_shape_colours: List[str] = []

    all_milling_stages = deepcopy(milling_stages)
    if background_milling_stages is not None:
        all_milling_stages.extend(deepcopy(background_milling_stages))
    n_milling_stages = len(milling_stages)

    # convert fibsem patterns to napari shapes
    for i, stage in enumerate(all_milling_stages):

        # shapes for this milling stage
        napari_shapes: List[np.ndarray]  = []
        shape_types: List[str] = []

        # TODO: QUERY  migrate to using label layers for everything??
        # TODO: re-enable annulus drawing, re-enable bitmaps
        for pattern_settings in stage.pattern.define():

            napari_drawing_fn = NAPARI_DRAWING_FUNCTIONS.get(type(pattern_settings), None)
            if napari_drawing_fn is None:
                logging.warning(f"Pattern type {type(pattern_settings)} not supported")
                continue

            shape = napari_drawing_fn(pattern_settings=pattern_settings, 
                                      shape=image_shape, 
                                      pixelsize=pixelsize)
            stype = NAPARI_PATTERN_LAYER_TYPES.get(type(pattern_settings), None)     
            napari_shapes.append(shape)
            shape_types.append(stype)

        # draw the patterns as a shape layer
        if napari_shapes:

            if draw_crosshair:
                crosshair_shapes = create_crosshair_shape(centre_point=stage.pattern.point,
                                                          shape=image_shape,
                                                          pixelsize=pixelsize)
                crosshair_shape_types = ["rectangle", "rectangle"]
                napari_shapes.extend(crosshair_shapes)
                shape_types.extend(crosshair_shape_types)

            is_background = i >= n_milling_stages
            if is_background:
                napari_colours = ["black"] * len(napari_shapes)
            else:
                napari_colours = [COLOURS[i % len(COLOURS)]] * len(napari_shapes)

            # TODO: properties dict for all parameters
            all_napari_shapes.extend(napari_shapes)
            all_shape_types.extend(shape_types)
            all_shape_colours.extend(napari_colours)

    name = "Milling Patterns"
    if all_napari_shapes:
        if name in viewer.layers:
            viewer.layers[name].data = [] # need to clear data before updating, to account for different shapes.
            viewer.layers[name].data = all_napari_shapes
            viewer.layers[name].shape_type = all_shape_types
            viewer.layers[name].edge_color = all_shape_colours
            viewer.layers[name].face_color = all_shape_colours
            viewer.layers[name].translate = translation
        else:
            viewer.add_shapes(
                data=all_napari_shapes,
                name=name,
                shape_type=all_shape_types,
                edge_width=SHAPES_LAYER_PROPERTIES["edge_width"],
                edge_color=all_shape_colours,
                face_color=all_shape_colours,
                opacity=SHAPES_LAYER_PROPERTIES["opacity"],
                blending=SHAPES_LAYER_PROPERTIES["blending"],
                translate=translation,
            )

    # remove all un-updated layers (assume they have been deleted)
    remove_all_napari_shapes_layers(viewer=viewer,
                                    layer_type=NapariShapesLayers,
                                    ignore=[name])

    return [name] # list of milling pattern layers

def convert_point_to_napari(resolution: list, pixel_size: float, centre: Point):
    icy, icx = resolution[1] // 2, resolution[0] // 2

    cx = int(icx + (centre.x / pixel_size))
    cy = int(icy - (centre.y / pixel_size))

    return Point(cx, cy)

def validate_pattern_placement(
    image_shape: Tuple[int, int], shape: List[List[float]]
):
    """Validate that the pattern shapes are within the image resolution"""
    x_lim = image_shape[1]
    y_lim = image_shape[0]

    for coordinate in shape:
        x_coord = coordinate[1]
        y_coord = coordinate[0]

        if x_coord < 0 or x_coord > x_lim:
            return False
        if y_coord < 0 or y_coord > y_lim:
            return False

    return True

def is_pattern_placement_valid(pattern: BasePattern, image: FibsemImage) -> bool:
    """Check if the pattern is within the image bounds."""

    if isinstance(pattern, FiducialPattern):
        _, is_not_valid_placement = calculate_fiducial_area_v2(image=image, 
                                            fiducial_centre = deepcopy(pattern.point), 
                                            fiducial_length = pattern.height)
        return not is_not_valid_placement
    
    for pattern_settings in pattern.define():
        draw_func = NAPARI_DRAWING_FUNCTIONS.get(type(pattern_settings), None)
        if draw_func is None:
            logging.warning(f"Pattern type {type(pattern_settings)} not supported")
            return False
        
        napari_shape = draw_func(pattern_settings=pattern_settings, 
                                 shape=image.data.shape, 
                                 pixelsize=image.metadata.pixel_size.x)
        is_valid_placement = validate_pattern_placement(image_shape=image.data.shape, 
                                                        shape=napari_shape)

        if not is_valid_placement:
            return False
    
    return True

def convert_reduced_area_to_napari_shape(reduced_area: FibsemRectangle, image_shape: Tuple[int, int]) -> np.ndarray:
    """Convert a reduced area to a napari shape."""
    x0 = reduced_area.left * image_shape[1]
    y0 = reduced_area.top * image_shape[0]
    x1 = x0 + reduced_area.width * image_shape[1]
    y1 = y0 + reduced_area.height * image_shape[0]
    data = [[y0, x0], [y0, x1], [y1, x1], [y1, x0]]
    return np.array(data)

def convert_shape_to_image_area(shape: List[List[int]], image_shape: Tuple[int, int]) -> FibsemRectangle:
    """Convert a napari shape (rectangle) to  a FibsemRectangle expressed as a percentage of the image (reduced area)
    shape: the coordinates of the shape
    image_shape: the shape of the image (usually the ion beam image)    
    """
    # get limits of rectangle
    y0, x0 = shape[0]
    y1, x1 = shape[1]
    """
        0################1
        |               |
        |               |
        |               |
        3################2
    """
    # get min/max coordinates
    x_coords = [x[1] for x in shape]
    y_coords = [x[0] for x in shape]
    x0, x1 = min(x_coords), max(x_coords)
    y0, y1 = min(y_coords), max(y_coords)

    logging.debug(f"convert shape data: {x0}, {x1}, {y0}, {y1}, fib shape: {image_shape}")
        
    # convert to percentage of image
    x0 = x0 / image_shape[1]
    x1 = x1 / image_shape[1]
    y0 = y0 / image_shape[0]
    y1 = y1 / image_shape[0]
    w = x1 - x0
    h = y1 - y0

    reduced_area = FibsemRectangle(left=x0, top=y0, width=w, height=h)
    logging.debug(f"reduced area: {reduced_area}")

    return reduced_area