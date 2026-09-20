"""Importable SDK lifecycle hooks: all accelerator ownership stays in the child."""


def load_model():
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError("Managed PYMSS requires its assigned CUDA GPU")
    from .native import NativeBackend
    return NativeBackend()


def completion(model):
    import torch
    if torch.cuda.is_initialized():
        torch.cuda.synchronize()


def release(model):
    model.close()


def cleanup():
    import gc
    gc.collect()
