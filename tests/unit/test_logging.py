# Copyright 2021 The Trieste Contributors
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
from __future__ import annotations

import tempfile

import tensorflow as tf

from trieste.logging import get_tensorboard_writer, set_tensorboard_writer, using_tensorboard_writer


def test_get_tensorboard_writer_default() -> None:
    assert get_tensorboard_writer() is None


def test_set_get_tensorboard_writer() -> None:
    with tempfile.TemporaryDirectory() as tmpdirname:
        summary_writer = tf.summary.create_file_writer(tmpdirname)
        set_tensorboard_writer(summary_writer)
        assert get_tensorboard_writer() is summary_writer
        set_tensorboard_writer(None)
        assert get_tensorboard_writer() is None


def test_using_tensorboard_writer() -> None:
    with tempfile.TemporaryDirectory() as tmpdirname:
        summary_writer = tf.summary.create_file_writer(tmpdirname)
        assert get_tensorboard_writer() is None
        with using_tensorboard_writer(summary_writer):
            assert get_tensorboard_writer() is summary_writer
        assert get_tensorboard_writer() is None
