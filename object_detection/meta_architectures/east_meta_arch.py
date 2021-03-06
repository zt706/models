# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""SSD Meta-architecture definition.

General tensorflow implementation of convolutional Multibox/SSD detection
models.
"""
from abc import abstractmethod

import re
import tensorflow as tf

from object_detection.core import box_list
from object_detection.core import box_predictor as bpredictor
from object_detection.core import model
from object_detection.core import standard_fields as fields
from object_detection.core import box_list_ops
from object_detection.core.matcher import Match
from object_detection.utils import variables_helper

slim = tf.contrib.slim


class EASTFeatureExtractor(object):
  """EAST Feature Extractor definition."""

  def __init__(self,
               depth_multiplier,
               min_depth,
               conv_hyperparams,
               reuse_weights=None):
    self._depth_multiplier = depth_multiplier
    self._min_depth = min_depth
    self._conv_hyperparams = conv_hyperparams
    self._reuse_weights = reuse_weights

  @abstractmethod
  def preprocess(self, resized_inputs):
    """Preprocesses images for feature extraction (minus image resizing).

    Args:
      resized_inputs: a [batch, height, width, channels] float tensor
        representing a batch of images.

    Returns:
      preprocessed_inputs: a [batch, height, width, channels] float tensor
        representing a batch of images.
    """
    pass

  @abstractmethod
  def extract_features(self, preprocessed_inputs):
    """Extracts features from preprocessed inputs.

    This function is responsible for extracting feature maps from preprocessed
    images.

    Args:
      preprocessed_inputs: a [batch, height, width, channels] float tensor
        representing a batch of images.

    Returns:
      feature_maps: a list of tensors where the ith tensor has shape
        [batch, height_i, width_i, depth_i]
    """
    pass


class EASTMetaArch(model.DetectionModel):
  """SSD Meta-architecture definition."""

  def __init__(self,
               is_training,
               anchor_generator,
               box_predictor,
               box_coder,
               feature_extractor,
               image_resizer_fn,
               non_max_suppression_fn,
               score_conversion_fn,
               classification_loss,
               localization_loss,
               classification_loss_weight,
               localization_loss_weight,
               normalize_loss_by_num_matches,
               add_summaries=True):
    """SSDMetaArch Constructor.

    TODO: group NMS parameters + score converter into
    a class and loss parameters into a class and write config protos for
    postprocessing and losses.

    Args:
      is_training: A boolean indicating whether the training version of the
        computation graph should be constructed.
      anchor_generator: an anchor_generator.AnchorGenerator object.
      box_predictor: a box_predictor.BoxPredictor object.
      box_coder: a box_coder.BoxCoder object.
      feature_extractor: a SSDFeatureExtractor object.
      matcher: a matcher.Matcher object.
      region_similarity_calculator: a
        region_similarity_calculator.RegionSimilarityCalculator object.
      image_resizer_fn: a callable for image resizing.  This callable always
        takes a rank-3 image tensor (corresponding to a single image) and
        returns a rank-3 image tensor, possibly with new spatial dimensions.
        See builders/image_resizer_builder.py.
      non_max_suppression_fn: batch_multiclass_non_max_suppression
        callable that takes `boxes`, `scores` and optional `clip_window`
        inputs (with all other inputs already set) and returns a dictionary
        hold tensors with keys: `detection_boxes`, `detection_scores`,
        `detection_classes` and `num_detections`. See `post_processing.
        batch_multiclass_non_max_suppression` for the type and shape of these
        tensors.
      score_conversion_fn: callable elementwise nonlinearity (that takes tensors
        as inputs and returns tensors).  This is usually used to convert logits
        to probabilities.
      classification_loss: an object_detection.core.losses.Loss object.
      localization_loss: a object_detection.core.losses.Loss object.
      classification_loss_weight: float
      localization_loss_weight: float
      normalize_loss_by_num_matches: boolean
      add_summaries: boolean (default: True) controlling whether summary ops
        should be added to tensorflow graph.
    """
    super(EASTMetaArch, self).__init__(num_classes=box_predictor.num_classes)
    self._is_training = is_training

    # Needed for fine-tuning from classification checkpoints whose
    # variables do not have the feature extractor scope.
    self._extract_features_scope = 'FeatureExtractor'

    self._anchor_generator = anchor_generator
    self._box_predictor = box_predictor

    self._box_coder = box_coder
    self._feature_extractor = feature_extractor
    self._classification_loss = classification_loss
    self._localization_loss = localization_loss
    self._classification_loss_weight = classification_loss_weight
    self._localization_loss_weight = localization_loss_weight
    self._normalize_loss_by_num_matches = normalize_loss_by_num_matches

    self._image_resizer_fn = image_resizer_fn
    self._non_max_suppression_fn = non_max_suppression_fn
    self._score_conversion_fn = score_conversion_fn

    self._anchors = None
    self._add_summaries = add_summaries

  @property
  def anchors(self):
    if not self._anchors:
      raise RuntimeError('anchors have not been constructed yet!')
    if not isinstance(self._anchors, box_list.BoxList):
      raise RuntimeError('anchors should be a BoxList object, but is not.')
    return self._anchors

  def preprocess(self, inputs):
    """Feature-extractor specific preprocessing.

    See base class.

    Args:
      inputs: a [batch, height_in, width_in, channels] float tensor representing
        a batch of images with values between 0 and 255.0.

    Returns:
      preprocessed_inputs: a [batch, height_out, width_out, channels] float
        tensor representing a batch of images.
    Raises:
      ValueError: if inputs tensor does not have type tf.float32
    """
    if inputs.dtype is not tf.float32:
      raise ValueError('`preprocess` expects a tf.float32 tensor')
    with tf.name_scope('Preprocessor'):
      # TODO: revisit whether to always use batch size as  the number of
      # parallel iterations vs allow for dynamic batching.
      #resized_inputs = tf.map_fn(self._image_resizer_fn,
      #                           elems=inputs,
      #                           dtype=tf.float32)
      return self._feature_extractor.preprocess(inputs)

  def predict(self, preprocessed_inputs):
    """Predicts unpostprocessed tensors from input tensor.

    This function takes an input batch of images and runs it through the forward
    pass of the network to yield unpostprocessesed predictions.

    A side effect of calling the predict method is that self._anchors is
    populated with a box_list.BoxList of anchors.  These anchors must be
    constructed before the postprocess or loss functions can be called.

    Args:
      preprocessed_inputs: a [batch, height, width, channels] image tensor.

    Returns:
      prediction_dict: a dictionary holding "raw" prediction tensors:
        1) box_encodings: 4-D float tensor of shape [batch_size, num_anchors,
          box_code_dimension] containing predicted boxes.
        2) rotations: 3-D float tensor of shape [batch_size, num_anchors, 1]
        3) scores: 3-D float tensor of shape [batch_size, num_anchors, 1]
        4) feature_maps: a list of tensors where the ith tensor has shape
          [batch, height_i, width_i, depth_i].
    """
    with tf.variable_scope(None, self._extract_features_scope,
                           [preprocessed_inputs]):
      feature_maps = self._feature_extractor.extract_features(
          preprocessed_inputs)
    if self._add_summaries:
      tf.summary.image("Loss/image", preprocessed_inputs)

    feature_map_shape = tf.shape(feature_maps[0])
    image_shape = tf.shape(preprocessed_inputs)
    self._anchors = self._anchor_generator.generate(
        [(feature_map_shape[1], feature_map_shape[2])])
    (box_encodings, rotation_encodings, score_encodings
    ) = self._add_box_predictions_to_feature_maps(feature_maps)
    predictions_dict = {
        'box_encodings': box_encodings,
		'rotations': rotation_encodings,
        'scores': score_encodings,
        'image_shape': image_shape,
        'feature_maps': feature_maps
    }
    return predictions_dict

  def _add_box_predictions_to_feature_maps(self, feature_maps):
    """Adds box predictors to each feature map and returns concatenated results.

    Args:
      feature_maps: a list of tensors where the ith tensor has shape
        [batch, height_i, width_i, depth_i]

    Returns:
      box_encodings: 4-D float tensor of shape [batch_size, num_anchors,
          box_code_dimension] containing predicted boxes.
      class_predictions_with_background: 2-D float tensor of shape
          [batch_size, num_anchors, num_classes+1] containing class predictions
          (logits) for each of the anchors.  Note that this tensor *includes*
          background class predictions (at class index 0).

    Raises:
      RuntimeError: if the number of feature maps extracted via the
        extract_features method does not match the length of the
        num_anchors_per_locations list that was passed to the constructor.
      RuntimeError: if box_encodings from the box_predictor does not have
        shape of the form  [batch_size, num_anchors, 1, code_size].
    """
    num_anchors_per_location_list = (
        self._anchor_generator.num_anchors_per_location())
    if len(feature_maps) != len(num_anchors_per_location_list):
      raise RuntimeError('the number of feature maps must match the '
                         'length of self.anchors.NumAnchorsPerLocation().')
    box_encodings_list = []
    rotation_encodings_list = []
    score_encodings_list = []
    for idx, (feature_map, num_anchors_per_location
             ) in enumerate(zip(feature_maps, num_anchors_per_location_list)):
      box_predictor_scope = 'BoxPredictor_{}'.format(idx)
      box_predictions = self._box_predictor.predict(feature_map,
                                                    num_anchors_per_location,
                                                    box_predictor_scope)
      box_encodings = box_predictions[bpredictor.BOX_ENCODINGS]
      rotation_encodings = box_predictions[bpredictor.ANGLE_ENCODINGS]
      score_encodings = box_predictions[bpredictor.SCORE_PREDICTIONS]
      if self._add_summaries:
        tf.summary.histogram("Pred/top", box_encodings[:,:,:,0])
        tf.summary.histogram("Pred/left", box_encodings[:,:,:,1])
        tf.summary.histogram("Pred/down", box_encodings[:,:,:,2])
        tf.summary.histogram("Pred/right", box_encodings[:,:,:,3])
        tf.summary.histogram("Pred/left-right", box_encodings[:,:,:,1] - box_encodings[:,:,:,3])
        tf.summary.histogram("Pred/top-down", box_encodings[:,:,:,0] - box_encodings[:,:,:,2])
        tf.summary.histogram("Pred/rotations", rotation_encodings)
        tf.summary.histogram("Pred/score", score_encodings)

      box_encodings_shape = box_encodings.get_shape().as_list()
      if len(box_encodings_shape) != 4 or box_encodings_shape[2] != 1:
        raise RuntimeError('box_encodings from the box_predictor must be of '
                           'shape `[batch_size, num_anchors, 1, code_size]`; '
                           'actual shape', box_encodings_shape)
      box_encodings = tf.squeeze(box_encodings, axis=2)
      rotation_encodings = tf.squeeze(rotation_encodings, axis=2)
      box_encodings_list.append(box_encodings)
      rotation_encodings_list.append(rotation_encodings)
      score_encodings_list.append(score_encodings)

    num_predictions = sum(
        [tf.shape(box_encodings)[1] for box_encodings in box_encodings_list])
    num_anchors = self.anchors.num_boxes()
    anchors_assert = tf.assert_equal(num_anchors, num_predictions, [
        'Mismatch: number of anchors vs number of predictions', num_anchors,
        num_predictions
    ])
    with tf.control_dependencies([anchors_assert]):
      box_encodings = tf.concat(box_encodings_list, 1)
      rotation_encodings = tf.concat(rotation_encodings_list, 1)
      score_encodings = tf.concat(score_encodings_list, 1)
    return box_encodings, rotation_encodings, score_encodings

  def score_filter(self, boxes, scores, score_thresh=0.5, max_detections=8192):
    from object_detection.utils import shape_utils

    if scores.shape.ndims != 2:
      raise ValueError('scores field must be of rank 2')
    if boxes.shape.ndims != 3:
      raise ValueError('boxes must be of rank 3.')
    if boxes.shape[2].value != 5:
      raise ValueError('boxes must be of shape [batch, anchors, 5].')

    with tf.name_scope('ScoreFilter'):
      per_image_boxes_list = tf.unstack(boxes)
      per_image_scores_list = tf.unstack(scores)
      detection_boxes_list = []
      detection_scores_list = []
      detection_classes_list = []
      num_detections_list = []
      for (per_image_boxes, per_image_scores
          ) in zip(per_image_boxes_list, per_image_scores_list):
        greater_indexes = tf.cast(tf.reshape(
            tf.where(tf.greater(per_image_scores, score_thresh)),
            [-1]), tf.int32)
        filterd_boxes = tf.gather(per_image_boxes, greater_indexes)
        filterd_scores = tf.gather(per_image_scores, greater_indexes)

        pad_boxes = shape_utils.pad_or_clip_tensor(filterd_boxes, max_detections)
        pad_scores = shape_utils.pad_or_clip_tensor(filterd_scores, max_detections)
        num_detections_list.append(tf.to_float(tf.shape(greater_indexes)[0]))
        detection_boxes_list.append(pad_boxes)
        detection_scores_list.append(pad_scores)
        detection_classes_list.append(tf.ones_like(pad_scores))

      det_dict = {
          'detection_boxes': tf.stack(detection_boxes_list),
          'detection_scores': tf.stack(detection_scores_list),
          'detection_classes': tf.stack(detection_classes_list),
          'num_detections': tf.stack(num_detections_list)
      }
      return det_dict


  def postprocess(self, prediction_dict):
    """Converts prediction tensors to final detections.

    This function converts raw predictions tensors to final detection results by
    slicing off the background class, decoding box predictions and applying
    non max suppression and clipping to the image window.

    See base class for output format conventions.  Note also that by default,
    scores are to be interpreted as logits, but if a score_conversion_fn is
    used, then scores are remapped (and may thus have a different
    interpretation).

    Args:
      prediction_dict: a dictionary holding prediction tensors with
        1) box_encodings: 4-D float tensor of shape [batch_size, num_anchors,
          box_code_dimension] containing predicted boxes.
        2) class_predictions_with_background: 2-D float tensor of shape
          [batch_size, num_anchors, num_classes+1] containing class predictions
          (logits) for each of the anchors.  Note that this tensor *includes*
          background class predictions.

    Returns:
      detections: a dictionary containing the following fields
        detection_boxes: [batch, max_detection, 4]
        detection_scores: [batch, max_detections]
        detection_classes: [batch, max_detections]
        num_detections: [batch]
    Raises:
      ValueError: if prediction_dict does not contain `box_encodings` or
        `class_predictions_with_background` fields.
    """
    if ('box_encodings' not in prediction_dict or
        'rotations' not in prediction_dict or
        'scores' not in prediction_dict):
      raise ValueError('prediction_dict does not contain expected entries.')
    with tf.name_scope('Postprocessor'):
      box_encodings = prediction_dict['box_encodings']
      score_predictions = prediction_dict['scores'] # [batch_size, num_anchors, 1]
      batched_rotations = tf.squeeze(prediction_dict['rotations'],
                                     axis=2) # [batch_size, num_anchors]
      detection_boxes = tf.stack([
          self._box_coder.decode(boxes, rotations, self.anchors).get()
          for boxes,rotations in zip(tf.unstack(box_encodings),
                                     tf.unstack(batched_rotations))
          ]) # [batch_size, num_anchors, 4]

      detection_scores = self._score_conversion_fn(score_predictions)

      self._non_max_suppression_fn = None
      if self._non_max_suppression_fn is None:
        detection_boxes = tf.concat([detection_boxes,
                                     prediction_dict['rotations']], -1) # [batch_size, num_anchors, 5]
        detection_scores = tf.squeeze(detection_scores, 2) # [batch_size, num_anchors]
        detections = self.score_filter(detection_boxes, detection_scores)
      else:
        detection_boxes = tf.expand_dims(detection_boxes, axis=2)
        clip_window = None #tf.constant([0, 0, 1, 1], tf.float32)
        detections = self._non_max_suppression_fn(detection_boxes,
                                                detection_scores,
                                                clip_window=clip_window)
      # TO DO: append rotations to box coordinates
    return detections

  def loss(self, prediction_dict, scope=None):
    """Compute scalar loss tensors with respect to provided groundtruth.

    Calling this function requires that groundtruth tensors have been
    provided via the provide_groundtruth function.

    Args:
      prediction_dict: a dictionary holding prediction tensors with
        1) box_encodings: 4-D float tensor of shape [batch_size, num_anchors,
          box_code_dimension] containing predicted boxes.
        2) class_predictions_with_background: 2-D float tensor of shape
          [batch_size, num_anchors, num_classes+1] containing class predictions
          (logits) for each of the anchors.  Note that this tensor *includes*
          background class predictions.
      scope: Optional scope name.

    Returns:
      a dictionary mapping loss keys (`localization_loss` and
        `classification_loss`) to scalar tensors representing corresponding loss
        values.
    """
    with tf.name_scope(scope, 'Loss', prediction_dict.values()):
      groundtruth_boxlists = self._format_groundtruth_data(prediction_dict['image_shape'])
      (batch_score_targets, batch_score_weights,
       batch_rbox_targets, batch_rbox_weights,
       match_list) = self._assign_targets(
           groundtruth_boxlists,
           self.groundtruth_lists(fields.BoxListFields.rotations),
           self.groundtruth_lists(fields.BoxListFields.masks))
      if self._add_summaries:
        self._summarize_input([bl.get() for bl in groundtruth_boxlists],
                              self.groundtruth_lists(fields.BoxListFields.masks),
                              match_list)
      num_matches = tf.stack(
          [match.num_matched_columns() for match in match_list])
      top_left = prediction_dict['box_encodings'][:,:,0:2] * -1.0
      down_right = prediction_dict['box_encodings'][:,:,2:4]
      predicted_rbox = tf.concat([top_left,
                                  down_right,
                                  prediction_dict['rotations']], -1)
      rbox_losses = self._localization_loss(
          predicted_rbox,
          batch_rbox_targets,
          weights=batch_rbox_weights)
      predicted_score = tf.squeeze(prediction_dict['scores'], axis=2)
      score_losses = self._classification_loss(
          predicted_score,
          batch_score_targets,
          weights=batch_score_weights)

      rbox_loss = tf.reduce_sum(rbox_losses)
      score_loss = tf.reduce_sum(score_losses)

      # Optionally normalize by number of positive matches
      normalizer = tf.constant(1.0, dtype=tf.float32)
      if self._normalize_loss_by_num_matches:
        normalizer = tf.maximum(tf.to_float(tf.reduce_sum(num_matches)), 1.0)

      rbox_loss = (self._localization_loss_weight / normalizer) * rbox_loss
      score_loss = (self._classification_loss_weight / normalizer) * score_loss
      #score_loss = self._classification_loss_weight * score_loss

      rbox_loss = tf.identity(rbox_loss, name="rbox_loss")
      score_loss = tf.identity(score_loss, name="score_loss")
      loss_dict = {
          'localization_loss': rbox_loss,
          'classification_loss': score_loss
      }
    return loss_dict

  def _assign_targets(self, groundtruth_boxlists, groundtruth_rotations_list,
                      groundtruth_masks_list):
    """Assign groundtruth targets.

    Adds a background class to each one-hot encoding of groundtruth classes
    and uses target assigner to obtain regression and classification targets.

    Args:
      groundtruth_boxes_list: a list of 2-D tensors of shape [num_boxes, 4]
        containing coordinates of the groundtruth boxes.
          Groundtruth boxes are provided in [y_min, x_min, y_max, x_max]
          format and assumed to be normalized and clipped
          relative to the image window with y_min <= y_max and x_min <= x_max.
      groundtruth_classes_list: a list of 2-D one-hot (or k-hot) tensors of
        shape [num_boxes, num_classes] containing the class targets with the 0th
        index assumed to map to the first non-background class.

    Returns:
      batch_cls_targets: a tensor with shape [batch_size, num_anchors,
        num_classes],
      batch_cls_weights: a tensor with shape [batch_size, num_anchors],
      batch_reg_targets: a tensor with shape [batch_size, num_anchors,
        box_code_dimension]
      batch_reg_weights: a tensor with shape [batch_size, num_anchors],
      match_list: a list of matcher.Match objects encoding the match between
        anchors and groundtruth boxes for each image of the batch,
        with rows of the Match objects corresponding to groundtruth boxes
        and columns corresponding to anchors.
    """

    score_targets_list = []
    score_weights_list = []
    rbox_targets_list = []
    rbox_weights_list = []
    match_list = []
    for gt_boxes, gt_rotations, gt_masks in zip(groundtruth_boxlists,
        groundtruth_rotations_list, groundtruth_masks_list):

      match = self._match(self.anchors, gt_masks)
      (score_targets, score_weights) = self._assign_score_target(match)
      (rbox_targets, rbox_weights) = self._assign_rbox_target(self.anchors, gt_boxes,
                                                              gt_rotations, match)
      score_targets_list.append(score_targets)
      score_weights_list.append(score_weights)
      rbox_targets_list.append(rbox_targets)
      rbox_weights_list.append(rbox_weights)
      match_list.append(match)

    batch_score_targets = tf.stack(score_targets_list)
    batch_score_weights = tf.stack(score_weights_list)
    batch_rbox_targets = tf.stack(rbox_targets_list)
    batch_rbox_weights = tf.stack(rbox_weights_list)
    return (batch_score_targets, batch_score_weights, batch_rbox_targets,
        batch_rbox_weights, match_list)

  def _match(self, anchors, groundtruth_masks):
    if not isinstance(anchors, box_list.BoxList):
      raise ValueError('anchors must be an BoxList')

    (ycenter, xcenter, height, width) = anchors.get_center_coordinates_and_sizes()
    # Note: anchors use absolute coordinates, so as the groundtruth_boxes
    yindices = tf.cast(tf.floor(ycenter), tf.int32)
    xindices = tf.cast(tf.floor(xcenter), tf.int32)
    coordinates = tf.stack([yindices, xindices], -1)
    groundtruth_masks = tf.cast(groundtruth_masks, tf.int32)
    groundtruth_masks = tf.Print(groundtruth_masks, [tf.shape(groundtruth_masks)], message='groundtruth_masks:') ###
    gt_masks_max = tf.reduce_max(groundtruth_masks, 0)
    gt_masks_argmax = tf.cast(tf.argmax(groundtruth_masks, 0), tf.int32)
    matched_obj_indices = tf.where(tf.greater(gt_masks_max, 0),
                                   gt_masks_argmax,
                                   -1*tf.ones_like(gt_masks_max))
    return Match(tf.gather_nd(matched_obj_indices, coordinates))

  def _assign_score_target(self, match):
    '''
    Args:
      groundtruth_masks:

    Returns:
      score_targets: a float32 tensor with shape [num_anchors],
      score_weights: a float32 tensor with shape [num_anchors]
    '''
    score_targets = tf.cast(match.matched_column_indicator(), tf.float32)
    matched_indicator = tf.cast(match.matched_column_indicator(), tf.float32)
    unmatched_indicator = 1.0 - matched_indicator
    num_matched = tf.cast(match.num_matched_columns(), tf.float32)
    num_unmatched = tf.cast(match.num_unmatched_columns(), tf.float32)
    beta = 1.0 - num_matched / (num_matched + num_unmatched)
    score_weights = beta * matched_indicator + (1.0 - beta) * unmatched_indicator
    return score_targets, score_weights

  def _assign_rbox_target(self, anchors, groundtruth_boxes, groundtruth_rotations,
                          match):
    '''
    Args:
      anchors: a BoxList representing N anchors
      groundtruth_boxes: a BoxList representing M groundtruth boxes
      groundtruth_rotations:

    Returns:
      reg_targets: a float32 tensor with shape [num_anchors, 5]
      reg_weights: a float32 tensor with shape [num_anchors]
    '''

    if not isinstance(anchors, box_list.BoxList):
      raise ValueError('anchors must be an BoxList')
    if not isinstance(groundtruth_boxes, box_list.BoxList):
      raise ValueError('groundtruth_boxes must be an BoxList')

    matched_anchor_indices = match.matched_column_indices()
    unmatched_ignored_anchor_indices = (match.
                                        unmatched_or_ignored_column_indices())
    matched_gt_indices = match.matched_row_indices()
    matched_anchors = box_list_ops.gather(anchors,
                                          matched_anchor_indices)
    matched_gt_boxes = box_list_ops.gather(groundtruth_boxes,
                                           matched_gt_indices)
    matched_gt_rotations = tf.gather(groundtruth_rotations,
                                     matched_gt_indices)
    matched_reg_targets = self._box_coder.encode(matched_gt_boxes,
                                                 matched_gt_rotations,
                                                 matched_anchors)
    default_target = tf.constant([5*[0]], tf.float32)
    unmatched_ignored_reg_targets = tf.tile(
        default_target,
        tf.stack([tf.size(unmatched_ignored_anchor_indices), 1]))
    reg_targets = tf.dynamic_stitch(
        [matched_anchor_indices, unmatched_ignored_anchor_indices],
        [matched_reg_targets, unmatched_ignored_reg_targets])
    reg_weights = tf.cast(match.matched_column_indicator(), tf.float32)
    return reg_targets, reg_weights

  def _format_groundtruth_data(self, image_shape):
    groundtruth_boxlists = [
        box_list_ops.to_absolute_coordinates(
            box_list.BoxList(boxes), image_shape[1], image_shape[2], check_range=False)
        for boxes in self.groundtruth_lists(fields.BoxListFields.boxes)]
    return groundtruth_boxlists

  def _summarize_input(self, groundtruth_boxes_list, groundtruth_masks_list, match_list):
    """Creates tensorflow summaries for the input boxes and anchors.

    This function creates four summaries corresponding to the average
    number (over images in a batch) of (1) groundtruth boxes, (2) anchors
    marked as positive, (3) anchors marked as negative, and (4) anchors marked
    as ignored.

    Args:
      groundtruth_boxes_list: a list of 2-D tensors of shape [num_boxes, 4]
        containing corners of the groundtruth boxes.
      match_list: a list of matcher.Match objects encoding the match between
        anchors and groundtruth boxes for each image of the batch,
        with rows of the Match objects corresponding to groundtruth boxes
        and columns corresponding to anchors.
    """
    num_boxes_per_image = tf.stack(
        [tf.shape(x)[0] for x in groundtruth_boxes_list])
    pos_anchors_per_image = tf.stack(
        [match.num_matched_columns() for match in match_list])
    neg_anchors_per_image = tf.stack(
        [match.num_unmatched_columns() for match in match_list])
    tf.summary.scalar('Input/AvgNumGroundtruthBoxesPerImage',
                      tf.reduce_mean(tf.to_float(num_boxes_per_image)))
    tf.summary.scalar('Input/AvgNumPositiveAnchorsPerImage',
                      tf.reduce_mean(tf.to_float(pos_anchors_per_image)))
    tf.summary.scalar('Input/AvgNumNegativeAnchorsPerImage',
                      tf.reduce_mean(tf.to_float(neg_anchors_per_image)))
    tf.summary.scalar('Input/AvgNumAnchorsPerImage',
                      tf.reduce_mean(tf.to_float(pos_anchors_per_image
                                                 + neg_anchors_per_image)))

    gt_masks = groundtruth_masks_list[0]
    gt_masks = tf.cast(gt_masks, tf.float32)
    gt_masks_max = tf.reduce_max(gt_masks, 0)
    mask = tf.expand_dims(gt_masks_max, 0)
    mask = tf.expand_dims(mask, -1)
    tf.summary.image("mask", mask)


  def restore_fn(self, checkpoint_path, from_detection_checkpoint=True):
    """Return callable for loading a checkpoint into the tensorflow graph.

    Args:
      checkpoint_path: path to checkpoint to restore.
      from_detection_checkpoint: whether to restore from a full detection
        checkpoint (with compatible variable names) or to restore from a
        classification checkpoint for initialization prior to training.

    Returns:
      a callable which takes a tf.Session as input and loads a checkpoint when
        run.
    """
    variables_to_restore = {}
    for variable in tf.all_variables():
      if variable.op.name.startswith(self._extract_features_scope):
        var_name = variable.op.name
        if not from_detection_checkpoint:
          var_name = (
              re.split('^' + self._extract_features_scope + '/', var_name)[-1])
        variables_to_restore[var_name] = variable
    # TODO: Load variables selectively using scopes.
    variables_to_restore = (
        variables_helper.get_variables_available_in_checkpoint(
            variables_to_restore, checkpoint_path))
    saver = tf.train.Saver(variables_to_restore)

    def restore(sess):
      saver.restore(sess, checkpoint_path)
    return restore
