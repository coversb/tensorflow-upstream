import os
import argparse
import tensorflow as tf

from tensorflow.python.distribute import reduce_util, collective_util, cross_device_ops
from tensorflow.python.framework import indexed_slices, ops
from tensorflow.python.distribute import values as value_lib
from tensorflow.python.ops import array_ops

# disable eager execution
# tf.compat.v1.disable_eager_execution()

# enable xla
tf.config.optimizer.set_jit(True)


def as_list(value):
    if isinstance(value, ops.Tensor):
        return [value]
    elif isinstance(value, indexed_slices.IndexedSlices):
        return [value]
    elif isinstance(value, value_lib.Mirrored):
        return value.values
    else:
        raise ValueError(
            "unwrap: unsupported input type: %s" % type(value))


def make_per_replica_value(value, devices):
    """Creates a `PerReplica` object whose values reside in `devices`.

    Args:
      value: a tensor-convertible value or a `IndexedSlicesValue`, or a callable
        that takes one argument (`device_idx`) and should return the value that is
        going to be created on devices[device_idx].
      devices: a list of device strings to create `PerReplica` values on.

    Returns:
      A `PerReplica` object.
    """
    values = []
    for device_idx, device in enumerate(devices):
        if callable(value):
            v = value(device_idx)
        elif isinstance(value, list):
            v = value[device_idx]
        else:
            v = value
        if isinstance(v, indexed_slices.IndexedSlicesValue):
            with ops.device(device):
                values.append(
                    indexed_slices.IndexedSlices(
                        values=array_ops.identity(v.values),
                        indices=array_ops.identity(v.indices),
                        dense_shape=array_ops.identity(v.dense_shape)))
        else:
            with ops.device(device):
                values.append(array_ops.identity(v))
    return value_lib.PerReplica(values)


def make_collective(num_processes, gpu_per_process):
    """Returns collectives and other info to be used in tests.

    Args:
      num_processes: an integer indicating the number of processes that
        participate in the collective.
      gpu_per_process: number of GPUs (0 if no GPUs) used by each process.

    Returns:
     A tuple of (collective, devices, pid) where collective is a instance
     of `CollectiveAllReduce`, devices are a list of local devices (str)
     attached to the current process, and pid is the id of this process among
     all participant processes.
    """

    # cluster_resolver = cluster_resolver_lib.TFConfigClusterResolver()  # not needed locally
    # task_id=cluster_resolver.task_id
    task_id = 0
    devices = [
        "/job:localhost/replica:0/task:%d/device:CPU:0" % task_id
    ]
    if gpu_per_process > 0:
        devices = [
            "/job:localhost/replica:0/task:%d/device:GPU:%d" %
            (task_id, i) for i in range(gpu_per_process)
        ]
    group_size = num_processes * len(devices)
    collective = cross_device_ops.CollectiveAllReduce(
        devices=devices,
        group_size=group_size,
        options=collective_util.Options())
    return collective, devices, task_id


def reduce_fn(input_tensor_list, collective, devices, pid,
              reduce_op=reduce_util.ReduceOp.SUM,
              communication_options=collective_util.Options(implementation=collective_util.CommunicationImplementation.NCCL)):
    def value_fn(
        device_idx): return input_tensor_list[pid * len(devices) + device_idx]
    per_replica_value = make_per_replica_value(value_fn, devices)
    reduced_values = collective.reduce(reduce_op, per_replica_value,
                                       per_replica_value,
                                       communication_options)
    reduced_values = as_list(reduced_values)
    return [ops.convert_to_tensor(v) for v in reduced_values]


def main(log_dir):
    # create input
    size = 4
    x = tf.random.uniform([size])
    data_1 = tf.slice(x, [0], [size//2])
    data_2 = tf.slice(x, [size//2], [-1])
    inputs = [data_1, data_2]
    print("Inputs:")
    for i in inputs:
        print(i)

    # get outputs
    num_processes = 1
    gpus_per_process = 2
    collective, devices, pid = make_collective(num_processes, gpus_per_process)
    outputs = reduce_fn(inputs, collective, devices, pid)
    print("Outputs:")
    for o in outputs:
        print(o)

    # write tf graph
    sess = tf.compat.v1.Session()
    tf.io.write_graph(sess.graph, log_dir, 'train.pbtxt')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--log_dir")
    args = parser.parse_args()

    main(args.log_dir)
