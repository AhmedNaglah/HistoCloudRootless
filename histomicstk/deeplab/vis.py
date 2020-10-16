# Lint as: python2, python3
# Copyright 2018 The TensorFlow Authors All Rights Reserved.
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

"""Segmentation results visualization on a given set of WSI.
See model.py for more details and usage.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import sys
sys.path.append("..")

import warnings
with warnings.catch_warnings():
    warnings.filterwarnings("ignore",category=FutureWarning)
    warnings.filterwarnings("ignore",category=RuntimeWarning)
    import os.path
    import time
    import numpy as np
    from six.moves import range
    import tensorflow as tf
    from tensorflow.contrib import quantize as contrib_quantize
    from tensorflow.contrib import training as contrib_training
    from deeplab import common
    from deeplab import model
    from deeplab.datasets import wsi_data_generator
    from deeplab.utils import save_annotation
    from deeplab.utils.wsi_dataset_util import get_slide_size
    from deeplab.utils.mask_to_xml import mask_to_xml
    from deeplab.utils.xml_to_json import convert_xml_json

flags = tf.app.flags

FLAGS = flags.FLAGS

flags.DEFINE_string('master', '', 'BNS name of the tensorflow server')

# Settings for log directories.

flags.DEFINE_string('vis_logdir', None, 'Where to write the event logs.')

flags.DEFINE_string('checkpoint_dir', None, 'Directory of model checkpoints.')

# Settings for visualizing the model.

flags.DEFINE_integer('vis_batch_size', 1,
                     'The number of images in each batch during evaluation.')

flags.DEFINE_integer('vis_crop_size', 512,
                  'Crop size [size, size] for visualization.')

flags.DEFINE_float('vis_remove_border', 0.15,
                   'Percent of the vis_crop_size border that is not used.')

flags.DEFINE_integer('eval_interval_secs', 60 * 5,
                     'How often (in seconds) to run evaluation.')

# For `xception_65`, use atrous_rates = [12, 24, 36] if output_stride = 8, or
# rates = [6, 12, 18] if output_stride = 16. For `mobilenet_v2`, use None. Note
# one could use different atrous_rates/output_stride during training/evaluation.
flags.DEFINE_multi_integer('atrous_rates', None,
                           'Atrous rates for atrous spatial pyramid pooling.')

flags.DEFINE_integer('output_stride', 16,
                     'The ratio of input to output spatial resolution.')

# Change to [0.5, 0.75, 1.0, 1.25, 1.5, 1.75] for multi-scale test.
flags.DEFINE_multi_float('eval_scales', [1.0],
                         'The scales to resize images for evaluation.')

# Change to True for adding flipped images during test.
flags.DEFINE_bool('add_flipped_images', False,
                  'Add flipped images for evaluation or not.')

flags.DEFINE_integer(
    'quantize_delay_step', -1,
    'Steps to start quantized training. If < 0, will not quantize model.')

# Dataset settings.

flags.DEFINE_string('dataset', 'wsi_dataset',
                    'Name of the segmentation dataset.')

flags.DEFINE_integer('wsi_downsample', 4,
                  'Downsample rate of WSI used during training.')

flags.DEFINE_integer('overlap_num', 4,
                  'Number of times the patch grid overlaps during testing.')

flags.DEFINE_integer('min_size', 0,
                  'Minimum size of the detected regions.')

flags.DEFINE_integer('num_classes', 2,
                  'Downsample rate of WSI used during training.')

flags.DEFINE_string('dataset_dir', None, 'Where the dataset reside.')

flags.DEFINE_enum('colormap_type', 'pascal', ['pascal', 'cityscapes', 'ade20k'],
                  'Visualization colormap type.')

flags.DEFINE_boolean('also_save_raw_predictions', False,
                     'Also save raw predictions.')

flags.DEFINE_boolean('save_json_annotation', False,
                     'Save the predictions in .json format for HistomicsTK.')

flags.DEFINE_string('json_filename', 'annotation.anot', '*.json annotation filename.')

# The folder where semantic segmentation predictions are saved.
_SEMANTIC_PREDICTION_SAVE_FOLDER = 'segmentation_results'

# The folder where raw semantic segmentation predictions are saved.
_RAW_SEMANTIC_PREDICTION_SAVE_FOLDER = 'raw_segmentation_results'

# The format to save image.
_IMAGE_FORMAT = '%06d_image'

# The format to save prediction
_PREDICTION_FORMAT = '%06d_prediction'


def _convert_train_id_to_eval_id(prediction, train_id_to_eval_id):
  """Converts the predicted label for evaluation.

  There are cases where the training labels are not equal to the evaluation
  labels. This function is used to perform the conversion so that we could
  evaluate the results on the evaluation server.

  Args:
    prediction: Semantic segmentation prediction.
    train_id_to_eval_id: A list mapping from train id to evaluation id.

  Returns:
    Semantic segmentation prediction whose labels have been changed.
  """
  converted_prediction = prediction.copy()
  for train_id, eval_id in enumerate(train_id_to_eval_id):
    converted_prediction[prediction == train_id] = eval_id

  return converted_prediction


def _process_batch(sess, slide_mask, semantic_predictions,
                   image_names, mask_size, border, downsample, image_heights,
                   image_widths, image_id_offset,
                   raw_save_dir, train_id_to_eval_id=None):
  """Evaluates one single batch qualitatively.

  Args:
    sess: TensorFlow session.
    semantic_predictions: One batch of semantic segmentation predictions.
    image_names: Image names.
    mask_size: [y,x] dimentions of the mask
    image_heights: Image heights.
    image_widths: Image widths.
    image_id_offset: Image id offset for indexing images.
    raw_save_dir: The directory where the raw predictions will be saved.
    train_id_to_eval_id: A list mapping from train id to eval id.
  """
  (semantic_predictions,
   image_names,
   image_heights,
   image_widths) = sess.run([semantic_predictions,
                             image_names, image_heights, image_widths])

  num_image = semantic_predictions.shape[0]
  for i in range(num_image):
    image_height = np.squeeze(image_heights[i])
    image_width = np.squeeze(image_widths[i])
    semantic_prediction = np.squeeze(semantic_predictions[i])
    crop_semantic_prediction = semantic_prediction[:image_height, :image_width]
    image_filename = image_names[i].decode()

    # populate wsi mask
    Ystart = float(image_filename.split('-')[-2])
    Ystart /= downsample
    Ystart = int(round(Ystart))+border

    Xstart = float(image_filename.split('-')[-3])
    Xstart /= downsample
    Xstart = int(round(Xstart))+border

    Ystop = min(Ystart+image_height-(border*2), mask_size[0])
    Xstop = min(Xstart+image_width-(border*2), mask_size[1])
    # print('\n')
    # print(mask_size)
    # print(border)
    # print(Xstart, Xstop, Ystart, Ystop)
    # print(Ystop-Ystart+border, Xstop-Xstart+border)
    # input('...')

    slide_mask[Ystart:Ystop, Xstart:Xstop] = np.maximum(
                slide_mask[Ystart:Ystop, Xstart:Xstop],
                semantic_prediction[border:Ystop-Ystart+border, border:Xstop-Xstart+border])

    if FLAGS.also_save_raw_predictions:
      if train_id_to_eval_id is not None:
        crop_semantic_prediction = _convert_train_id_to_eval_id(
            crop_semantic_prediction,
            train_id_to_eval_id)
      save_annotation.save_annotation(
          crop_semantic_prediction, raw_save_dir, image_filename,
          add_colormap=False)

  return slide_mask


def main(unused_argv):
  tf.logging.set_verbosity(tf.logging.INFO)

  # Get dataset-dependent information.
  dataset = wsi_data_generator.Dataset(
      dataset_name=FLAGS.dataset,
      dataset_dir=FLAGS.dataset_dir,
      num_of_classes=FLAGS.num_classes,
      downsample=FLAGS.wsi_downsample,
      overlap_num=FLAGS.overlap_num,
      batch_size=FLAGS.vis_batch_size,
      crop_size=FLAGS.vis_crop_size,
      min_resize_value=FLAGS.min_resize_value,
      max_resize_value=FLAGS.max_resize_value,
      resize_factor=FLAGS.resize_factor,
      model_variant=FLAGS.model_variant,
      is_training=False,
      should_shuffle=False,
      should_repeat=False)

  vis_border = int(round(FLAGS.vis_crop_size*FLAGS.vis_remove_border))
  assert vis_border * 2 < FLAGS.vis_crop_size

  if os.path.isfile(FLAGS.dataset_dir):
      slides = [FLAGS.dataset_dir]
  else:
      # get all WSI in test set
      slides = dataset._get_all_files(with_xml=False, save_mask=False)

  broken_slides = []

  for slide in slides:

      print('Working on: [{}]'.format(slide))

      try:
          # get slide size and create empty wsi mask
          slide_size = get_slide_size(slide)
          def get_downsampled_size(size, downsample=FLAGS.wsi_downsample):
              size /= downsample
              return int(np.ceil(size))
          mask_size = [get_downsampled_size(slide_size[1]), get_downsampled_size(slide_size[0])]
          slide_mask = np.zeros([slide_size[1], slide_size[0]], dtype=np.uint8)

      except:
          print('!!! Faulty slide: skipping [{}] !!!'.format(slide))
          broken_slides.append(slide)
          continue

      train_id_to_eval_id = None
      raw_save_dir = None

      if FLAGS.also_save_raw_predictions:
          raw_save_dir = os.path.join(
                FLAGS.vis_logdir, _RAW_SEMANTIC_PREDICTION_SAVE_FOLDER)
          # Prepare for visualization.
          tf.gfile.MakeDirs(FLAGS.vis_logdir)
          tf.gfile.MakeDirs(raw_save_dir)

      with tf.Graph().as_default():
        try:
            iterator, num_samples = dataset.get_one_shot_iterator_grid(slide)
        except:
            print('!!! Faulty slide: skipping [{}] !!!'.format(slide))
            broken_slides.append(slide)
            continue
        samples = iterator.get_next()

        model_options = common.ModelOptions(
            outputs_to_num_classes={common.OUTPUT_TYPE: dataset.num_of_classes},
            crop_size=[FLAGS.vis_crop_size,FLAGS.vis_crop_size],
            atrous_rates=FLAGS.atrous_rates,
            output_stride=FLAGS.output_stride)

        tf.logging.info('Performing WSI patch detection.\n')
        predictions = model.predict_labels(
              samples[common.IMAGE],
              model_options=model_options,
              image_pyramid=FLAGS.image_pyramid)

        predictions = predictions[common.OUTPUT_TYPE]

        tf.train.get_or_create_global_step()
        if FLAGS.quantize_delay_step >= 0:
          contrib_quantize.create_eval_graph()

        # checkpoints_iterator = contrib_training.checkpoints_iterator(
        #     FLAGS.checkpoint_dir, min_interval_secs=FLAGS.eval_interval_secs)
        # for checkpoint_path in checkpoints_iterator:
        checkpoint_path = FLAGS.checkpoint_dir
        # tf.logging.info(
        #     'Starting visualization at ' + time.strftime('%Y-%m-%d-%H:%M:%S',
        #                                                  time.gmtime()))
        # tf.logging.info('Visualizing with model %s', checkpoint_path)

        scaffold = tf.train.Scaffold(init_op=tf.global_variables_initializer())
        session_creator = tf.train.ChiefSessionCreator(
              scaffold=scaffold,
              master=FLAGS.master,
              checkpoint_filename_with_path=checkpoint_path)
        with tf.train.MonitoredSession(
                session_creator=session_creator, hooks=None) as sess:
            batch = 0
            image_id_offset = 0

            while not sess.should_stop():
              # tf.logging.info('Visualizing batch %d', batch + 1)
              print('\rWorking on [{}] patch: [{} of {}]'.format(os.path.basename(slide), batch, num_samples), end='')
              slide_mask = _process_batch(sess=sess,
                             slide_mask=slide_mask,
                             semantic_predictions=predictions,
                             image_names=samples[common.IMAGE_NAME],
                             mask_size=mask_size,
                             border=vis_border,
                             downsample=FLAGS.wsi_downsample,
                             image_heights=samples[common.HEIGHT],
                             image_widths=samples[common.WIDTH],
                             image_id_offset=image_id_offset,
                             raw_save_dir=raw_save_dir,
                             train_id_to_eval_id=train_id_to_eval_id)
              image_id_offset += FLAGS.vis_batch_size
              batch += FLAGS.vis_batch_size

      if FLAGS.save_json_annotation:
          anot_filename = FLAGS.json_filename
          print('\ncreating annotation file: [{}]'.format(anot_filename))
          root = mask_to_xml(xml_path=anot_filename, mask=slide_mask, downsample=FLAGS.wsi_downsample, min_size_thresh=FLAGS.min_size, return_root=True)
          json_data = convert_xml_json(root, ['gloms'])
          import json
          with open(anot_filename, 'w') as annotation_file:
              json.dump(json_data, annotation_file, indent=2, sort_keys=False)

      else:
          anot_filename = '{}.xml'.format(slide.split('.')[0])
          print('\ncreating annotation file: [{}]'.format(anot_filename))
          mask_to_xml(xml_path=anot_filename, mask=slide_mask, downsample=FLAGS.wsi_downsample, min_size_thresh=FLAGS.min_size, )

      print('annotation file saved...\n\n')
  if len(broken_slides) > 0:
      print('\n!!! The following slides are broken and were not run:')
      for slide in broken_slides:
          print('-\t[{}]'.format(slide))


if __name__ == '__main__':
  flags.mark_flag_as_required('checkpoint_dir')
  flags.mark_flag_as_required('dataset_dir')
  tf.app.run()
  print('\n\nall done.')
