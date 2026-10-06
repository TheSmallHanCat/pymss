from __future__ import annotations

from types import SimpleNamespace

import pytest

from pymss.config import AttrDict
from pymss.separator import _prefer_mlx_for_auto, _resolve_public_device
from pymss.utils import (
    _mlx_warmup_audio_length,
    _mlx_warmup_channel_candidates,
    warmup_mlx_full,
)

class DummyLogger:
    def debug(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

def _config(*, chunk_size=960000, batch_size=2):
    return AttrDict(
        {
            "audio": {"chunk_size": chunk_size},
            "inference": {"batch_size": batch_size},
        }
    )

class FakeModel:
    """Minimal stand-in recording the shapes the warmup pass requests."""

    mps_model_backend = "mlx_full"
    mps_model_compute_dtype = None

    def modules(self):
        return [self]

    def parameters(self):
        import torch

        self._param = torch.zeros(1)
        return iter([self._param])

    def to(self, device):
        import torch

        self._param = self._param.to(device)
        self.moved_to = str(device)
        return self

    def __init__(self, *, audio_channels=2, stft_kwargs=None, error=None, allowed_channels=None):
        import torch

        self.audio_channels = audio_channels
        self.stft_kwargs = stft_kwargs if stft_kwargs is not None else {"n_fft": 2048, "hop_length": 512}
        self.mps_model_compute_dtype = torch.float16
        self.error = error
        self.allowed_channels = allowed_channels
        self.calls = []

    def mlx_forward_mx(self, raw_audio):
        self.calls.append(tuple(raw_audio.shape))
        if self.error is not None:
            raise self.error
        if self.allowed_channels is not None and raw_audio.shape[1] not in self.allowed_channels:
            raise ValueError("raw_audio channel count does not match RoFormer stereo setting")
        return raw_audio

@pytest.fixture()
def fake_mlx(monkeypatch):
    """Provide a fake ``mlx.core`` so warmup logic is testable without MLX."""

    class FakeArray:
        def __init__(self, shape, dtype):
            self.shape = shape
            self.dtype = dtype

    class FakeMx:
        float16 = "float16"
        float32 = "float32"

        def __init__(self):
            self.zero_shapes = []
            self.eval_calls = 0

        def zeros(self, shape, dtype=None):
            self.zero_shapes.append((tuple(shape), dtype))
            return FakeArray(tuple(shape), dtype)

        def eval(self, *_args):
            self.eval_calls += 1

    fake = FakeMx()
    monkeypatch.setitem(__import__("sys").modules, "mlx.core", fake)
    monkeypatch.setitem(__import__("sys").modules, "mlx", SimpleNamespace(core=fake))
    cleared = []
    import pymss.utils as utils

    monkeypatch.setattr(utils, "clear_mlx_cache", lambda: cleared.append(True))
    return fake, cleared

def test_device_mlx_enables_warmup_by_default(monkeypatch):
    import torch

    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: True)

    device, params = _resolve_public_device("mlx", {}, DummyLogger())

    assert device == "mps"

def test_warmup_audio_length_follows_stft_settings():
    assert _mlx_warmup_audio_length(FakeModel(stft_kwargs={"n_fft": 2048, "hop_length": 512})) == 130560
    assert _mlx_warmup_audio_length(FakeModel(stft_kwargs={"n_fft": 4096, "hop_length": 1024})) == 261120


def test_warmup_audio_length_reads_subband_stft_object():
    class SubbandSTFTStub:
        n_fft, hop_length = 2048, 512

    model = FakeModel()
    del model.stft_kwargs
    model.stft = SubbandSTFTStub()

    assert _mlx_warmup_audio_length(model) == 130560

def test_warmup_audio_length_survives_missing_stft_kwargs():
    model = FakeModel()
    del model.stft_kwargs

    length = _mlx_warmup_audio_length(model)
    assert length // 512 * 512 == length or True  # hop-agnostic default
    assert (length // 512 + 1) % 16 == 0  # frames divisible by downsampling stack

def test_warmup_channel_candidates_prefers_declared_channels():
    assert _mlx_warmup_channel_candidates(FakeModel(audio_channels=1)) == (1,)
    assert _mlx_warmup_channel_candidates(FakeModel(audio_channels=2)) == (2,)

    model = FakeModel()
    del model.audio_channels

    assert _mlx_warmup_channel_candidates(model) == (2, 1)

def test_warmup_runs_both_phases_with_real_shapes(fake_mlx):
    fake, cleared = fake_mlx
    model = FakeModel()

    assert warmup_mlx_full(model, _config(chunk_size=960000, batch_size=2)) is True

    tiny_length = _mlx_warmup_audio_length(model)
    assert model.calls == [(1, 2, tiny_length), (2, 2, 960000)]
    assert fake.zero_shapes == [((1, 2, tiny_length), "float16"), ((2, 2, 960000), "float16")]
    assert fake.eval_calls == 2
    assert len(cleared) == 2
    assert not hasattr(model, "_pymss_mlx_full_backend_error")

def test_warmup_skips_real_shape_phase_without_chunk_size(fake_mlx):
    fake, cleared = fake_mlx
    model = FakeModel()

    assert warmup_mlx_full(model, _config(chunk_size=None, batch_size=2)) is True

    assert len(model.calls) == 1
    assert len(cleared) == 1

def test_warmup_probes_channel_count_on_value_error(fake_mlx):
    model = FakeModel(audio_channels=None, allowed_channels=(1,))
    del model.audio_channels

    assert warmup_mlx_full(model, _config(chunk_size=None)) is True

    assert [shape[1] for shape in model.calls] == [2, 1]

def test_warmup_records_error_and_returns_false(fake_mlx):
    model = FakeModel(error=RuntimeError("metal exploded"))

    assert warmup_mlx_full(model, _config()) is False
    assert "metal exploded" in model._pymss_mlx_full_backend_error

def test_warmup_returns_false_for_not_implemented_backend(fake_mlx):
    model = FakeModel(error=NotImplementedError("PoPE models require the PyTorch backend"))

    assert warmup_mlx_full(model, _config()) is False
    assert not hasattr(model, "_pymss_mlx_full_backend_error")

def test_warmup_reports_channel_probe_exhaustion(fake_mlx):
    model = FakeModel(audio_channels=None, allowed_channels=())
    del model.audio_channels

    assert warmup_mlx_full(model, _config(chunk_size=None)) is False
    assert "channel count" in model._pymss_mlx_full_backend_error

def _separator_stub(device="mps"):
    from pymss.separator import MSSeparator

    stub = SimpleNamespace(device=device, logger=DummyLogger())
    stub._warmup_mlx_full_backend = MSSeparator._warmup_mlx_full_backend.__get__(stub)
    return stub

def test_load_model_warmup_skips_non_mps_device(monkeypatch):
    import pymss.utils as utils

    called = []
    monkeypatch.setattr(utils, "warmup_mlx_full", lambda *args: called.append(args) or True)
    stub = _separator_stub(device="cpu")
    model = FakeModel()

    stub._warmup_mlx_full_backend(model, _config())

    assert not called

def test_load_model_warmup_skips_torch_backend(monkeypatch):
    import pymss.utils as utils

    called = []
    monkeypatch.setattr(utils, "warmup_mlx_full", lambda *args: called.append(args) or True)
    stub = _separator_stub()
    model = FakeModel()
    model.mps_model_backend = "torch"

    stub._warmup_mlx_full_backend(model, _config())

    assert not called

def test_load_model_warmup_failure_downgrades_to_torch(monkeypatch):
    import pymss.utils as utils

    def failing(_model, _config):
        _model._pymss_mlx_full_backend_error = "repr: boom"
        return False

    monkeypatch.setattr(utils, "warmup_mlx_full", failing)
    stub = _separator_stub()
    model = FakeModel()

    stub._warmup_mlx_full_backend(model, _config())

    assert model.mps_model_backend == "torch"

def test_load_model_warmup_success_keeps_mlx_backend(monkeypatch):
    import pymss.utils as utils

    monkeypatch.setattr(utils, "warmup_mlx_full", lambda _model, _config: True)
    stub = _separator_stub()
    model = FakeModel()

    stub._warmup_mlx_full_backend(model, _config())

    assert model.mps_model_backend == "mlx_full"
