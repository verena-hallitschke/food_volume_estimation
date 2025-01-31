import argparse
import numpy as np
import cv2
import tensorflow as tf
from keras.models import Model, model_from_json
from food_volume_estimation.volume_estimator import VolumeEstimator, DensityDatabase
from food_volume_estimation.depth_estimation.custom_modules import *
from food_volume_estimation.food_segmentation.food_segmentator import FoodSegmentator
from flask import Flask, request, jsonify, make_response, abort
import base64


app = Flask(__name__)
estimator = None

def load_volume_estimator(depth_model_architecture, depth_model_weights,
        segmentation_model_weights, relaxation_param: float = 0.01):
    """Loads volume estimator object and sets up its parameters."""
    # Create estimator object and intialize
    global estimator
    estimator = VolumeEstimator(arg_init=False)
    with open(depth_model_architecture, 'r') as read_file:
        custom_losses = Losses()
        objs = {'ProjectionLayer': ProjectionLayer,
                'ReflectionPadding2D': ReflectionPadding2D,
                'InverseDepthNormalization': InverseDepthNormalization,
                'AugmentationLayer': AugmentationLayer,
                'compute_source_loss': custom_losses.compute_source_loss}
        model_architecture_json = json.load(read_file)
        estimator.monovideo = model_from_json(model_architecture_json,
                                              custom_objects=objs)
    estimator._VolumeEstimator__set_weights_trainable(estimator.monovideo,
                                                      False)
    estimator.monovideo.load_weights(depth_model_weights)
    estimator.model_input_shape = (
        estimator.monovideo.inputs[0].shape.as_list()[1:])
    depth_net = estimator.monovideo.get_layer('depth_net')
    estimator.depth_model = Model(inputs=depth_net.inputs,
                                  outputs=depth_net.outputs,
                                  name='depth_model')
    print('[*] Loaded depth estimation model.')

    # Depth model configuration
    MIN_DEPTH = 0.01
    MAX_DEPTH = 10
    estimator.min_disp = 1 / MAX_DEPTH
    estimator.max_disp = 1 / MIN_DEPTH
    estimator.gt_depth_scale = 0.35 # Ground truth expected median depth

    # Create segmentator object
    estimator.segmentator = FoodSegmentator(segmentation_model_weights)
    # Set plate adjustment relaxation parameter
    estimator.relax_param = relaxation_param

    # Need to define default graph due to Flask multiprocessing
    global graph
    graph = tf.get_default_graph()


@app.route('/predict', methods=['POST'])
def volume_estimation():
    """Receives an HTTP multipart request and returns the estimated 
    volumes of the foods in the image given.

    Multipart form data:
        img: The image file to estimate the volume in.
        plate_diameter: The expected plate diamater to use for depth scaling.
        If omitted then no plate scaling is applied.

    Returns:
        The array of estimated volumes in JSON format.
    """
    # Decode incoming byte stream to get an image
    try:
        content = request.json
        img_encoded = content['img']
        jpg_original = base64.b64decode(img_encoded)
        print("Test 1")
        img_arr = np.frombuffer(jpg_original, dtype=np.uint8)
        print("Test 2")
        img = cv2.imdecode(img_arr, cv2.IMREAD_COLOR)
        print("Test 3")
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    except Exception as e:
        print(e)
        abort(406)


    # Get expected plate diameter from form data or set to 0 and ignore
    try:
        plate_diameter = float(content['plate_diameter'])
    except Exception as e:
        plate_diameter = 0

    # Estimate volumes
    with graph.as_default():
        volumes = estimator.estimate_volume(img, fov=content.get("fov", 70),
            plate_diameter_prior=plate_diameter)
    # Convert to mL
    volumes = [v * 1e6 for v in volumes]

    # Return values
    return_vals = {
        'volumes': volumes,
    }
    return make_response(jsonify(return_vals), 200)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Food volume estimation API.')
    parser.add_argument('--depth_model_architecture', type=str,
                        help='Path to depth model architecture (.json).',
                        metavar='/path/to/architecture.json',
                        required=True)
    parser.add_argument('--depth_model_weights', type=str,
                        help='Path to depth model weights (.h5).',
                        metavar='/path/to/depth/weights.h5',
                        required=True)
    parser.add_argument('--segmentation_model_weights', type=str,
                        help='Path to segmentation model weights (.h5).',
                        metavar='/path/to/segmentation/weights.h5',
                        required=True)
    parser.add_argument('--relaxation_param', type=float,
                        help='Plate adjustment relaxation parameter.',
                        metavar='<relaxation_param>',
                        default=0.01)
    parser.add_argument('--port', type=int,
                        help='Port the app will run on.',
                        metavar='<port>',
                        default=8080)
    args = parser.parse_args()

    load_volume_estimator(args.depth_model_architecture,
                          args.depth_model_weights, 
                          args.segmentation_model_weights,
                          relaxation_param=args.relaxation_param)
    app.run(host='0.0.0.0', port=args.port)

