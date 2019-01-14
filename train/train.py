from __future__ import print_function

import os
import sys
import argparse
import importlib
import numpy as np
import tensorflow as tf
import pickle
from threading import Thread
from datetime import datetime
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
sys.path.append(BASE_DIR)
sys.path.append(os.path.join(ROOT_DIR, 'models'))
sys.path.append(os.path.join(ROOT_DIR, 'dataset'))
from kitti import Dataset
import train_util
from model_util import NUM_FG_POINT

parser = argparse.ArgumentParser()
parser.add_argument('--gpu', type=int, default=0, help='GPU to use [default: GPU 0]')
parser.add_argument('--model', default='frustum_pointnets_v1', help='Model name [default: frustum_pointnets_v1]')
parser.add_argument('--log_dir', default='log', help='Log dir [default: log]')
parser.add_argument('--num_point', type=int, default=2048, help='Point Number [default: 2048]')
parser.add_argument('--max_epoch', type=int, default=201, help='Epoch to run [default: 201]')
parser.add_argument('--batch_size', type=int, default=32, help='Batch Size during training [default: 32]')
parser.add_argument('--learning_rate', type=float, default=0.001, help='Initial learning rate [default: 0.001]')
parser.add_argument('--momentum', type=float, default=0.9, help='Initial learning rate [default: 0.9]')
parser.add_argument('--optimizer', default='adam', help='adam or momentum [default: adam]')
parser.add_argument('--decay_step', type=int, default=200000, help='Decay step for lr decay [default: 200000]')
parser.add_argument('--decay_rate', type=float, default=0.7, help='Decay rate for lr decay [default: 0.7]')
parser.add_argument('--no_intensity', action='store_true', help='Only use XYZ for training')
parser.add_argument('--restore_model_path', default=None, help='Restore model path e.g. log/model.ckpt [default: None]')
FLAGS = parser.parse_args()

# Set training configurations
EPOCH_CNT = 0
BATCH_SIZE = FLAGS.batch_size
NUM_POINT = FLAGS.num_point
MAX_EPOCH = FLAGS.max_epoch
BASE_LEARNING_RATE = FLAGS.learning_rate
GPU_INDEX = FLAGS.gpu
MOMENTUM = FLAGS.momentum
OPTIMIZER = FLAGS.optimizer
DECAY_STEP = FLAGS.decay_step
DECAY_RATE = FLAGS.decay_rate

MODEL = importlib.import_module(FLAGS.model) # import network module
MODEL_FILE = os.path.join(ROOT_DIR, 'models', FLAGS.model+'.py')
LOG_DIR = FLAGS.log_dir
if not os.path.exists(LOG_DIR): os.mkdir(LOG_DIR)
os.system('cp %s %s' % (MODEL_FILE, LOG_DIR)) # bkp of model def
os.system('cp %s %s' % (os.path.join(BASE_DIR, 'train.py'), LOG_DIR))
LOG_FOUT = open(os.path.join(LOG_DIR, 'log_train.txt'), 'w')
LOG_FOUT.write(str(FLAGS)+'\n')

BN_INIT_DECAY = 0.5
BN_DECAY_DECAY_RATE = 0.5
BN_DECAY_DECAY_STEP = float(DECAY_STEP)
BN_DECAY_CLIP = 0.99

def log_string(out_str):
    LOG_FOUT.write(out_str+'\n')
    LOG_FOUT.flush()
    print(out_str)

def get_learning_rate(batch):
    learning_rate = tf.train.exponential_decay(
                        BASE_LEARNING_RATE,  # Base learning rate.
                        batch * BATCH_SIZE,  # Current index into the dataset.
                        DECAY_STEP,          # Decay step.
                        DECAY_RATE,          # Decay rate.
                        staircase=True)
    learing_rate = tf.maximum(learning_rate, 0.00001) # CLIP THE LEARNING RATE!
    return learning_rate

def get_bn_decay(batch):
    bn_momentum = tf.train.exponential_decay(
                      BN_INIT_DECAY,
                      batch*BATCH_SIZE,
                      BN_DECAY_DECAY_STEP,
                      BN_DECAY_DECAY_RATE,
                      staircase=True)
    bn_decay = tf.minimum(BN_DECAY_CLIP, 1 - bn_momentum)
    return bn_decay

TRAIN_DATASET = Dataset(NUM_POINT, '/data/ssd/public/jlliu/Kitti/object', 'train')
TEST_DATASET = Dataset(NUM_POINT, '/data/ssd/public/jlliu/Kitti/object', 'val')

def train():
    ''' Main function for training and simple evaluation. '''
    # data loading threads
    train_produce_thread = Thread(target=TRAIN_DATASET.load, args=('/data/ssd/public/jlliu/PointRCNN/dataset/train',))
    train_produce_thread.start()
    test_produce_thread = Thread(target=TEST_DATASET.load, args=('/data/ssd/public/jlliu/PointRCNN/dataset/val',))
    test_produce_thread.start()

    with tf.Graph().as_default():
        with tf.device('/gpu:'+str(GPU_INDEX)):
            pointclouds_pl, mask_labels_pl, objectness_pl, \
            center_bin_x_pl, center_bin_z_pl,\
            center_x_residuals_pl, center_z_residuals_pl, center_y_residuals_pl, heading_bin_pl,\
            heading_residuals_pl, size_class_pl, size_residuals_pl \
                = MODEL.placeholder_inputs(BATCH_SIZE, NUM_POINT)

            is_training_pl = tf.placeholder(tf.bool, shape=())

            # Note the global_step=batch parameter to minimize.
            # That tells the optimizer to increment the 'batch' parameter
            # for you every time it trains.
            batch = tf.get_variable('batch', [],
                initializer=tf.constant_initializer(0), trainable=False)
            bn_decay = get_bn_decay(batch)
            tf.summary.scalar('bn_decay', bn_decay)

            # Get model and losses
            end_points = {}
            labels_pl = {
                'mask_label': mask_labels_pl,
                'objectness': objectness_pl,
                'center_bin_x': center_bin_x_pl,
                'center_bin_z': center_bin_z_pl,
                'center_x_residuals': center_x_residuals_pl,
                'center_z_residuals': center_z_residuals_pl,
                'center_y_residuals': center_y_residuals_pl,
                'heading_bin': heading_bin_pl,
                'heading_residuals': heading_residuals_pl,
                'size_class': size_class_pl,
                'size_residuals': size_residuals_pl
            }
            end_points = MODEL.get_model(pointclouds_pl, mask_labels_pl,
                is_training_pl, bn_decay, end_points)
            loss = MODEL.get_loss(labels_pl, end_points)

            iou2ds, iou3ds = tf.py_func(train_util.compute_box3d_iou, [
                end_points['fg_points'], end_points['fg_point_indices'],
                end_points['heading_scores'], end_points['heading_residuals_normalized'],
                end_points['size_scores'], end_points['size_residuals_normalized'],
                end_points['center_x_scores'], end_points['center_z_scores'],
                end_points['center_x_residuals_normalized'], end_points['center_y_residuals'],
                end_points['center_z_residuals_normalized'],
                labels_pl['heading_bin'], labels_pl['heading_residuals'], labels_pl['size_class'], labels_pl['size_residuals'],
                labels_pl['center_bin_x'], labels_pl['center_bin_z'],
                labels_pl['center_x_residuals'], labels_pl['center_y_residuals'], labels_pl['center_z_residuals']
                ],
                [tf.float32, tf.float32])
            end_points['iou2ds'] = iou2ds
            end_points['iou3ds'] = iou3ds

            # Get training operator
            learning_rate = get_learning_rate(batch)
            tf.summary.scalar('learning_rate', learning_rate)
            if OPTIMIZER == 'momentum':
                optimizer = tf.train.MomentumOptimizer(learning_rate,
                    momentum=MOMENTUM)
            elif OPTIMIZER == 'adam':
                optimizer = tf.train.AdamOptimizer(learning_rate)

            # Note: when training, the moving_mean and moving_variance need to be updated.
            update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
            with tf.control_dependencies(update_ops):
                train_op = optimizer.minimize(loss, global_step=batch)

            # Add ops to save and restore all the variables.
            saver = tf.train.Saver()

        # Create a session
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        config.allow_soft_placement = True
        config.log_device_placement = False
        sess = tf.Session(config=config)

        # Add summary writers
        merged = tf.summary.merge_all()
        train_writer = tf.summary.FileWriter(os.path.join(LOG_DIR, 'train'), sess.graph)
        test_writer = tf.summary.FileWriter(os.path.join(LOG_DIR, 'test'), sess.graph)

        # Init variables
        if FLAGS.restore_model_path is None:
            init = tf.global_variables_initializer()
            sess.run(init)
        else:
            saver.restore(sess, FLAGS.restore_model_path)

        ops = {
            'pointclouds_pl': pointclouds_pl,
            'is_training_pl': is_training_pl,
            'loss': loss,
            'train_op': train_op,
            'step': batch,
            'merged': merged,
            'end_points': end_points}
        ops.update(labels_pl)

        for epoch in range(MAX_EPOCH):
            log_string('**** EPOCH %03d ****' % (epoch))
            sys.stdout.flush()

            train_one_epoch(sess, ops, train_writer)
            #if epoch % 3 == 0:
            val_loss = eval_one_epoch(sess, ops, test_writer)
            # Save the variables to disk.
            # if val_loss < best_val_loss:
            #     best_val_loss = val_loss
            #     save_path = saver.save(sess, os.path.join(LOG_DIR, "model.ckpt"))
            #     log_string("Model saved in file: {0}, val_loss: {1}".format(save_path, val_loss))
            save_path = saver.save(sess, os.path.join(LOG_DIR, "model.ckpt.%03d" % epoch))
            log_string("Model saved in file: {0}".format(save_path))
    TRAIN_DATASET.stop_loading()
    TEST_DATASET.stop_loading()
    train_produce_thread.join()
    test_produce_thread.join()


def train_one_epoch(sess, ops, train_writer):
    is_training = True
    log_string(str(datetime.now()))

    # To collect statistics
    total_correct = 0
    total_seen = 0
    total_tp = 0
    total_fp = 0
    total_fn = 0
    loss_sum = 0
    iou2ds_sum = 0
    iou3ds_sum = 0

    # Training with batches
    # for batch_idx in range(num_batches):
    batch_idx = 0
    while(True):
        batch_pc, batch_mask_label, batch_objectness, \
        batch_center_bin_x, batch_center_bin_z, batch_center_x_residuals, \
        batch_center_y_residuals, batch_center_z_residuals, batch_heading_bin, \
        batch_heading_residuals, batch_size_class, batch_size_residuals, \
        is_last_batch = TRAIN_DATASET.get_next_batch(BATCH_SIZE)

        feed_dict = {
            ops['pointclouds_pl']: batch_pc,
            ops['mask_label']: batch_mask_label,
            ops['objectness']: batch_objectness,
            ops['center_bin_x']: batch_center_bin_x,
            ops['center_bin_z']: batch_center_bin_z,
            ops['center_x_residuals']: batch_center_x_residuals,
            ops['center_y_residuals']: batch_center_y_residuals,
            ops['center_z_residuals']: batch_center_z_residuals,
            ops['heading_bin']: batch_heading_bin,
            ops['heading_residuals']: batch_heading_residuals,
            ops['size_class']: batch_size_class,
            ops['size_residuals']: batch_size_residuals,
            ops['is_training_pl']: is_training,
        }
        summary, step, loss_val, _, logits_val, iou2ds, iou3ds = sess.run([
            ops['merged'], ops['step'], ops['loss'], ops['train_op'],
            ops['end_points']['foreground_logits'],
            ops['end_points']['iou2ds'], ops['end_points']['iou3ds']], feed_dict=feed_dict)

        train_writer.add_summary(summary, step)

        # segmentation acc
        preds_val = np.argmax(logits_val, 2)
        correct = np.sum(preds_val == batch_mask_label)
        tp = np.sum(np.logical_and(preds_val == batch_mask_label, batch_mask_label == 1))
        fp = np.sum(np.logical_and(preds_val != batch_mask_label, batch_mask_label == 0))
        fn = np.sum(np.logical_and(preds_val != batch_mask_label, batch_mask_label == 1))
        total_correct += correct
        total_tp += tp
        total_fp += fp
        total_fn += fn
        total_seen += NUM_POINT * BATCH_SIZE
        loss_sum += loss_val
        iou2ds_sum += np.sum(iou2ds)
        iou3ds_sum += np.sum(iou3ds)

        if (batch_idx+1)%10 == 0:
            log_string(' -- %03d --' % (batch_idx+1))
            log_string('mean loss: %f' % (loss_sum / 10))
            if total_seen > 0:
                log_string('segmentation accuracy: %f' % \
                    (total_correct / float(total_seen)))
            if total_tp+total_fn > 0 and total_tp+total_fp > 0:
                log_string('segmentation recall: %f'% \
                    (float(total_tp)/(total_tp+total_fn)))
                log_string('segmentation precision: %f'% \
                    (float(total_tp)/(total_tp+total_fp)))
                log_string('box IoU (ground/3D): %f / %f' % \
                    (iou2ds_sum / float(NUM_FG_POINT*10), iou3ds_sum / float(NUM_FG_POINT*10)))
            total_correct = 0
            total_seen = 0
            total_tp = 0
            total_fp = 0
            total_fn = 0
            loss_sum = 0
            iou2ds_sum = 0
            iou3ds_sum = 0
        if is_last_batch:
            break
        batch_idx += 1


def eval_one_epoch(sess, ops, test_writer):
    global EPOCH_CNT
    is_training = False
    log_string(str(datetime.now()))
    log_string('---- EPOCH %03d EVALUATION ----'%(EPOCH_CNT))

    # To collect statistics
    total_correct = 0
    total_seen = 0
    total_tp = 0
    total_fp = 0
    total_fn = 0
    loss_sum = 0
    num_batches = 0
    iou2ds_sum = 0
    iou3ds_sum = 0

    while(True):
        batch_pc, batch_mask_label, batch_objectness, \
        batch_center_bin_x, batch_center_bin_z, batch_center_x_residuals, \
        batch_center_y_residuals, batch_center_z_residuals, batch_heading_bin, \
        batch_heading_residuals, batch_size_class, batch_size_residuals, \
        is_last_batch = TEST_DATASET.get_next_batch(BATCH_SIZE)

        feed_dict = {
            ops['pointclouds_pl']: batch_pc,
            ops['mask_label']: batch_mask_label,
            ops['objectness']: batch_objectness,
            ops['center_bin_x']: batch_center_bin_x,
            ops['center_bin_z']: batch_center_bin_z,
            ops['center_x_residuals']: batch_center_x_residuals,
            ops['center_y_residuals']: batch_center_y_residuals,
            ops['center_z_residuals']: batch_center_z_residuals,
            ops['heading_bin']: batch_heading_bin,
            ops['heading_residuals']: batch_heading_residuals,
            ops['size_class']: batch_size_class,
            ops['size_residuals']: batch_size_residuals,
            ops['is_training_pl']: is_training,
        }

        summary, step, loss_val, logits_val, iou2ds, iou3ds = sess.run([ops['merged'], ops['step'], ops['loss'],
            ops['end_points']['foreground_logits'],
            ops['end_points']['iou2ds'], ops['end_points']['iou3ds']], feed_dict=feed_dict)
        test_writer.add_summary(summary, step)

        # segmentation acc
        preds_val = np.argmax(logits_val, 2)
        correct = np.sum(preds_val == batch_mask_label)
        tp = np.sum(np.logical_and(preds_val == batch_mask_label, batch_mask_label == 1))
        fp = np.sum(np.logical_and(preds_val != batch_mask_label, batch_mask_label == 0))
        fn = np.sum(np.logical_and(preds_val != batch_mask_label, batch_mask_label == 1))
        total_tp += tp
        total_fp += fp
        total_fn += fn
        total_correct += correct
        total_seen += NUM_POINT * BATCH_SIZE
        loss_sum += loss_val
        num_batches += BATCH_SIZE
        iou2ds_sum += np.sum(iou2ds)
        iou3ds_sum += np.sum(iou3ds)
        if is_last_batch:
            break

    log_string('eval mean loss: %f' % (loss_sum / float(num_batches)))
    log_string('eval segmentation accuracy: %f'% \
        (total_correct / float(total_seen)))
    log_string('eval segmentation recall: %f'% \
        (float(total_tp)/(total_tp+total_fn)))
    log_string('eval segmentation precision: %f'% \
        (float(total_tp)/(total_tp+total_fp)))
    log_string('eval box IoU (ground/3D): %f / %f' % \
        (iou2ds_sum / float(NUM_FG_POINT*num_batches), iou3ds_sum / float(NUM_FG_POINT*num_batches)))
    EPOCH_CNT += 1
    return loss_sum / float(num_batches)

if __name__ == "__main__":
    log_string('pid: %s'%(str(os.getpid())))
    train()
    LOG_FOUT.close()