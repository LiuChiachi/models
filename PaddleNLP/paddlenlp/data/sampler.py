# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
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

import collections
import functools
import math
import six

import numpy as np
import paddle.distributed as dist


class SamplerHelper(object):
    """
    SamplerHelper is to help construct iterable sampler used for `DataLoader`. It wraps
    a dataset and uses its :code:`__getitem__`
    Every SamplerHelper subclass has to provide an :meth:`__iter__` method, providing a
    way to iterate over indices of dataset elements, and a :meth:`__len__` method
    that returns the length of the returned iterators.
    Also can be used as batch iterator instead of indices iterator when `iterator`
    yield samples rather than indices by initializing `iterator` with a iterable
    dataset.
    .. note:: The :meth:`__len__` method isn't strictly required by
              :class:`DataLoader`, but is expected in any
              calculation involving the length of a :class:`DataLoader`.
    Args:
        dataset (Dataset): Input dataset for SamplerHelper.
        iterable (collections.Iterable|callable, optional): Iterator of dataset. Default: None.
    """

    # chain sampler
    def __init__(self, dataset, iterable=None):
        self.data_source = dataset
        self.iterable = iterable
        if isinstance(dataset, collections.Iterable) and iterable is None:
            # iterable-style datasets
            self.iterable = dataset

    def __iter__(self):
        if self.iterable is None:
            return iter(range(len(self.data_source)))
        elif isinstance(self.iterable, collections.Iterable):
            return iter(self.iterable)
        elif callable(self.iterable):
            return self.iterable()
        else:
            raise ValueError(
                "`iterable` should be None, instance of Iterable or callable "
                "producing generator.")

    def __len__(self):
        # Allow some samplers have different length with `len(data_source)`,
        # such as batch sampler.
        if hasattr(self, "_length"):
            return self._length
        else:
            return len(self.data_source)

    @property
    def length(self):
        """
        Returns:
            the length of the SamplerHelper.
        """

        # since `len()` only produce integer, use length property to get None
        # for uncertain length. samplers can set length if necessary.
        try:
            length = len(self)
        except Exception:
            length = None
        return length

    @length.setter
    def length(self, length):
        self._length = length

    def apply(self, fn):
        """
        Transformations would be performed. It includes `Shuffle`, `sort`, `fit` and `shard`.
        Args:
            fn (callable): Transformations to be performed. It returns transformed iterable (and data_source).
        Returns:
            SamplerHelper: A new transformed object.
        """
        rs = fn(self)
        if isinstance(rs, (list, tuple)):
            iterable, data_source = rs
        else:
            iterable, data_source = rs, self.data_source
        sampler = type(self)(data_source, iterable)
        return sampler

    def shuffle(self, buffer_size=-1, seed=None):
        """
        Shuffle the dataset according to the given buffer size and random seed.
        Args:
            buffer_size (int): Buffer size for shuffle. if buffer_size < 0 or more than the length of the dataset, 
                buffer_size is the length of the dataset. Default: -1. 
            seed (int, optional): Seed for the random. Default: None.
        Returns:
            SamplerHelper
         """
        if seed is not None:
            random_generator = np.random.RandomState(seed)
        else:  # use the global random generator
            random_generator = np.random

        def _impl():
            buf = []
            for idx in iter(self):
                buf.append(idx)
                if buffer_size > 0 and len(buf) >= buffer_size:
                    random_generator.shuffle(buf)
                    for b in buf:
                        yield b
                    buf = []
            if len(buf) > 0:
                random_generator.shuffle(buf)
                for b in buf:
                    yield b

        return type(self)(self.data_source, _impl)

    def sort(self, cmp=None, key=None, reverse=False, buffer_size=-1):
        """
        Sort samples according to given callable cmp or key.
        Args:
            cmp (callable): The funcation of comparison. Default: None. 
            key (callable): Return element to be compared. Default: None.
            reverse (bool): If True, it means in descending order, and False means in ascending order. Default: False.
            buffer_size (int): Buffer size for sort. If buffer_size < 0 or buffer_size is more than the length of the data, 
                buffer_size will be set to the length of the data. Default: -1.
        Returns:
            SamplerHelper
        """
        if key:
            key_wrapper = (lambda x: key(x, self.data_source))
        elif cmp:
            key_wrapper = functools.cmp_to_key(
                lambda x, y: cmp(x, y, self.data_source))
        else:
            key_wrapper = (lambda x: len(self.data_source[x]))

        def _impl():
            data_source = self.data_source
            buf = []
            for idx in iter(self):
                buf.append(idx)
                if buffer_size > 0 and len(buf) >= buffer_size:
                    buf = sorted(buf, key=key_wrapper, reverse=reverse)
                    for b in buf:
                        yield b
                    buf = []
            if len(buf) > 0:
                buf = sorted(buf, key=key_wrapper, reverse=reverse)
                for b in buf:
                    yield b

        return type(self)(self.data_source, _impl)

    def batch(self,
              batch_size,
              drop_last=False,
              batch_size_fn=None,
              batch_fn=None,
              batch_by_token=False):
        """
        To produce a BatchSampler.
        Agrs:
            batch_size (int): Batch size.
            drop_last (bool): Whether to drop the last mini batch. Default: False.
            batch_size_fn (callable, optional): Return the size of mini batch so far. Default: None.
            batch_fn (callable, optional): Transformations to be performed. Default: None.
        Returns:
            SamplerHelper
        """
        ori_batch_size_fn = batch_size_fn
        if batch_size_fn is None:
            batch_size_fn = lambda new, count, sofar, max_len, data_source: count * max_len

        def _impl():
            data_source = self.data_source
            minibatch, size_so_far, max_len = [], 0, 0
            for idx in iter(self):
                minibatch.append(idx)
                cur_len = len(data_source[idx][0]) if batch_by_token else 1
                max_len = max(max_len, cur_len)
                size_so_far = batch_size_fn(idx,
                                            len(minibatch), size_so_far,
                                            max_len, data_source)
                if size_so_far == batch_size:
                    yield minibatch
                    minibatch, size_so_far, max_len = [], 0, 0
                elif size_so_far > batch_size:
                    if len(minibatch) == 1:
                        raise ValueError(
                            "Please increase the value of `batch_size`, or limit the max length of batch"
                        )
                    yield minibatch[:-1]
                    max_len = cur_len
                    minibatch, size_so_far = minibatch[-1:], batch_size_fn(
                        idx, 1, 0, max_len, data_source)
            if minibatch and not drop_last:
                yield minibatch

        sampler = type(self)(
            self.data_source,
            _impl) if batch_fn is None else self.apply(batch_fn)
        if ori_batch_size_fn is None and batch_fn is None and self.length is not None:
            sampler.length = (self.length + int(not drop_last) *
                              (batch_size - 1)) // batch_size
        else:
            sampler.length = None

        return sampler

    def shard(self, num_replicas=None, rank=None):
        """
        Operates slice using multi GPU.
        Args:
            num_replicas (int, optional): The number of training process, and is also the number of GPU cards used in training. 
                Default: None.
            rank (int, optional): Number of training process. Equal to the value of the environment variable PADDLE_TRAINER_ID.
                Default: None.
        Returns:
            SamplerHelper
        """
        if num_replicas is None:
            num_replicas = dist.get_world_size()
        if rank is None:
            rank = dist.get_rank()

        def _impl():
            for i, idx in enumerate(self):
                if i % num_replicas == rank:
                    yield idx
            if i % num_replicas != num_replicas - 1 and rank > i % num_replicas:
                # use last samples to make it evenly divisible
                yield idx

        sampler = type(self)(self.data_source, _impl)
        if self.length is not None:
            sampler.length = int(math.ceil(self.length * 1.0 / num_replicas))
        else:
            sampler.length = None
        return sampler

    def list(self):
        """
        Produce a sampler with a `listiterator` when calling `iter`. Since `list`
        would fetch all contents at time, thus it can get accurate length.
        Returns:
            SamplerHelper
        """

        def _impl():
            indices = list(iter(self))
            self.length = len(indices)
            return iter(indices)

        return type(self)(self.data_source, _impl)
