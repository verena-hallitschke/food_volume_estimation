import tensorflow as tf
import keras.backend as K
from keras.layers import Layer
from project import *
import json


class ProjectionLayer(Layer):
    """
    Projective inverse warping layer.
    Initialize with the camera intrinsics matrix which is kept constant
    during training.
    """
    def __init__(self, intrinsics_mat=None, **kwargs):
        self.POSE_SCALING = 0.01
        if intrinsics_mat is None:
            self.intrinsics_mat = np.array([[1, 0, 0.5],
                                            [0, 1, 0.5],
                                            [0, 0,   1]])
        else:
            self.intrinsics_mat = intrinsics_mat
        self.intrinsics_mat_inv = np.linalg.inv(self.intrinsics_mat)
        super(ProjectionLayer, self).__init__(**kwargs)

    def build(self, input_shape):
        self.intrinsics_mat_tensor = K.variable(self.intrinsics_mat)
        self.intrinsics_mat_inv_tensor = K.variable(self.intrinsics_mat_inv)
        super(ProjectionLayer, self).build(input_shape)

    def call(self, x):
        source_img = x[0]
        depth_map = x[1]
        pose = x[2] * self.POSE_SCALING
        reprojected_img, _ = inverse_warp(source_img, depth_map, pose,
                                          self.intrinsics_mat_tensor,
                                          self.intrinsics_mat_inv_tensor)
        return reprojected_img

    def compute_output_shape(self, input_shape):
        return input_shape[0]

    def get_config(self):
        config = {
            'intrinsics_mat': self.intrinsics_mat
        }
        base_config = super(ProjectionLayer, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class ReflectionPadding2D(Layer):
    """
    Reflection padding layer.
    Padding (p1,p2) is applied as ([p1 rows p1], [p2 cols p2]).
    """
    def __init__(self, padding=(1,1), **kwargs):
        self.padding = tuple(padding)
        super(ReflectionPadding2D, self).__init__(**kwargs)

    def call(self, x):
        return tf.pad(x, [[0,0], [self.padding[0], self.padding[0]],
                          [self.padding[1], self.padding[1]], [0,0]],
                      'REFLECT')

    def compute_output_shape(self, input_shape):
        return (input_shape[0], input_shape[1]+2*self.padding[0],
                input_shape[2]+2*self.padding[1], input_shape[3])

    def get_config(self):
        config = {
            'padding': self.padding
        }
        base_config = super(ReflectionPadding2D, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class InverseDepthNormalization(Layer):
    """
    Normalizes and inverses disparities to create depth map with
    given max and min values.
    """
    def __init__(self, min_depth=1, max_depth=100, **kwargs):
        self.min_depth = min_depth
        self.max_depth = max_depth
        self.min_disp = 1 / max_depth
        self.max_disp = 1 / min_depth
        super(InverseDepthNormalization, self).__init__(**kwargs)

    def call(self, x):
        normalized_disp = (self.min_disp
                           + (self.max_disp - self.min_disp) * x)
        depth_map = 1 / normalized_disp
        return depth_map

    def compute_output_shape(self, input_shape):
        return input_shape

    def get_config(self):
        config = {
            'min_depth': self.min_depth,
            'max_depth': self.max_depth
        }
        base_config = super(InverseDepthNormalization, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class Losses():
    def reprojection_loss(self, alpha=0.85, masking=True):
        """
        Creates reprojection loss function combining MAE and SSIM losses.
        The reprojection loss is computed per scale by choosing the minimum
        loss between the previous and next frame reprojections.
            Inputs:
                alpha: SSIM loss weight
            Outputs:
                reprojection_loss: Reprojection Keras-style loss function
        """
        def reprojection_loss_keras(y_true, y_pred):
            prev_frame = y_pred[:,:,:,:3]
            next_frame = y_pred[:,:,:,3:6]
            reprojection_prev = y_pred[:,:,:,6:9]
            reprojection_next = y_pred[:,:,:,9:12]

            # Reprojection MAE
            reprojection_prev_mae = K.mean(K.abs(y_true - reprojection_prev),
                                           axis=-1, keepdims=True)
            reprojection_next_mae = K.mean(K.abs(y_true - reprojection_next),
                                           axis=-1, keepdims=True)
            scale_min_mae = K.minimum(reprojection_prev_mae, 
                                      reprojection_next_mae)
            # Reprojection SSIM
            reprojection_prev_ssim = self.__ssim(y_true, reprojection_prev)
            reprojection_next_ssim = self.__ssim(y_true, reprojection_next)
            scale_min_ssim = K.minimum(reprojection_prev_ssim,
                                       reprojection_next_ssim)
            # Total loss
            reprojection_loss = (alpha * scale_min_ssim 
                                 + (1 - alpha) * scale_min_mae)

            if masking:
                # Source frame MAE
                prev_mae = K.mean(K.abs(y_true - prev_frame), axis=-1,
                                  keepdims=True)
                next_mae = K.mean(K.abs(y_true - next_frame), axis=-1,
                                  keepdims=True)
                source_min_mae  = K.minimum(prev_mae, next_mae)
                # Source frame SSIM
                prev_ssim = self.__ssim(y_true, prev_frame)
                next_ssim = self.__ssim(y_true, next_frame)
                source_min_ssim = K.minimum(prev_ssim, next_ssim)

                source_loss = (alpha * source_min_ssim
                               + (1 - alpha) * source_min_mae)
                mask = K.less(reprojection_loss, source_loss)
                reprojection_loss *= K.cast(mask, 'float32')

            return reprojection_loss

        return reprojection_loss_keras


    def depth_smoothness(self):
        """
        Computes image-aware depth smoothness loss.
        Taken from:
            https://github.com/tensorflow/models/tree/master/research/struct2depth
        Modified by Alexander Graikos.
        """
        def depth_smoothness_keras(y_true, y_pred):
            img = y_true
            # Normalize inverse depth by mean
            inverse_depth = y_pred / (tf.reduce_mean(y_pred, axis=[1,2,3], 
                                      keepdims=True) + 1e-7)
            # Compute depth smoothness loss
            inverse_depth_dx = self.__gradient_x(inverse_depth)
            inverse_depth_dy = self.__gradient_y(inverse_depth)
            image_dx = self.__gradient_x(img)
            image_dy = self.__gradient_y(img)
            weights_x = tf.exp(-tf.reduce_mean(tf.abs(image_dx), 3, 
                                               keepdims=True))
            weights_y = tf.exp(-tf.reduce_mean(tf.abs(image_dy), 3,
                                               keepdims=True))
            smoothness_x = inverse_depth_dx * weights_x
            smoothness_y = inverse_depth_dy * weights_y
            return (tf.reduce_mean(tf.abs(smoothness_x)) 
                    + tf.reduce_mean(tf.abs(smoothness_y)))

        return depth_smoothness_keras


    def __ssim(self, x, y):
        """
        Computes a differentiable structured image similarity measure.
        Taken from:
            https://github.com/tensorflow/models/tree/master/research/struct2depth
        Modified by Alexander Graikos.
        """
        c1 = 0.01**2  # As defined in SSIM to stabilize div. by small denom.
        c2 = 0.03**2
        # Add padding to maintain img size
        x = tf.pad(x, [[0,0], [1,1], [1,1], [0,0]], 'REFLECT')
        y = tf.pad(y, [[0,0], [1,1], [1,1], [0,0]], 'REFLECT')
        mu_x = K.pool2d(x, (3,3), (1,1), 'valid', pool_mode='avg')
        mu_y = K.pool2d(y, (3,3), (1,1), 'valid', pool_mode='avg')
        sigma_x = (K.pool2d(x**2, (3,3), (1,1), 'valid', pool_mode='avg')
                   - mu_x**2)
        sigma_y = (K.pool2d(y**2, (3,3), (1,1), 'valid', pool_mode='avg')
                   - mu_y**2)
        sigma_xy = (K.pool2d(x * y, (3,3), (1,1), 'valid', pool_mode='avg') 
                    - mu_x * mu_y)
        ssim_n = (2 * mu_x * mu_y + c1) * (2 * sigma_xy + c2)
        ssim_d = (mu_x**2 + mu_y**2 + c1) * (sigma_x + sigma_y + c2)
        ssim = ssim_n / ssim_d
        return K.clip((1 - ssim) / 2, 0, 1)


    def __gradient_x(self, img):
        return img[:, :, :-1, :] - img[:, :, 1:, :]


    def __gradient_y(self, img):
        return img[:, :-1, :, :] - img[:, 1:, :, :]


class NumpyEncoder(json.JSONEncoder):
    """
    JSON encoder for numpy types.
    """
    def default(self, obj):
        if isinstance(obj, (np.int_, np.intc, np.intp, np.int8,
                            np.int16, np.int32, np.int64, np.uint8,
                            np.uint16, np.uint32, np.uint64)):
            return int(obj)
        elif isinstance(obj, (np.float_, np.float16, np.float32, 
                              np.float64)):
            return float(obj)
        elif isinstance(obj,(np.ndarray,)):
            return obj.tolist()
        return json.JSONEncoder.default(self, obj)
