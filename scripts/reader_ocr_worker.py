"""Private offline GPU OCR; binary packets only, no credentials or disk images."""
import contextlib
import importlib.util
import inspect
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from comic_pipeline.onnx_worker import read_packet, write_packet


def nar_reader():
    import numpy as np
    import onnxruntime as ort
    from PIL import Image
    from comic_pipeline.gpu_devices import adapters, tested_adapter
    adapter = tested_adapter(adapters())
    if not adapter:
        raise RuntimeError("GPUUnavailable")
    folder = ROOT/".runtime/ocr-candidates/nar"
    spec = importlib.util.spec_from_file_location("reader_nar_preprocess", folder/"manga_ocr_nar_small/preprocess.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    buffer = module.empty_inputs()
    chars = json.loads((folder/"nar_vocabulary_6817.json").read_text(encoding="utf-8"))["characters"]
    options = ort.SessionOptions()
    options.intra_op_num_threads, options.inter_op_num_threads = 4, 1
    options.enable_mem_pattern = False
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.log_severity_level = 3
    session = ort.InferenceSession(str(folder/"onnx/mangaocr_nar_small_fp32.onnx"), options,
        providers=[("DmlExecutionProvider", {"device_id":adapter["index"]}), "CPUExecutionProvider"])
    session.disable_fallback()
    if "DmlExecutionProvider" not in session.get_providers():
        raise RuntimeError("GPUUnavailable")
    def read(array):
        module.preprocess(Image.fromarray(np.asarray(array, dtype=np.uint8)), buffer)
        ids = session.run(None, buffer)[0].reshape(-1).tolist()
        if len(chars) in ids:
            ids = ids[:ids.index(len(chars))]
        return "".join(chars[i] for i in ids if 0 <= i < len(chars))
    return read, dict(provider="DmlExecutionProvider", adapter=adapter["name"] if "name" in adapter else "P3000")


def paddle_reader():
    import numpy as np
    from PIL import Image
    import torch
    import torch_directml
    from transformers import AutoModelForCausalLM, AutoProcessor
    torch.set_num_threads(4)
    torch.privateuseone.disable_tiled_resources(True)
    index = next(i for i in range(torch_directml.device_count()) if "P3000" in torch_directml.device_name(i))
    device = torch_directml.device(index)
    folder = ROOT/".runtime/ocr-candidates/paddlevl-source"
    model = AutoModelForCausalLM.from_pretrained(str(folder), trust_remote_code=True,
        torch_dtype=torch.float16, local_files_only=True, low_cpu_mem_usage=True, attn_implementation="eager").eval()
    model.config._attn_implementation = model.config.vision_config._attn_implementation = "eager"
    model.to(device)
    mask_class = type(model.model)
    original_mask = mask_class._prepare_4d_causal_attention_mask_with_cache_position
    signature = inspect.signature(original_mask)
    def cpu_mask(*values, **kwargs):
        bound = signature.bind(*values, **kwargs)
        target = bound.arguments["device"]
        bound.arguments["device"] = torch.device("cpu")
        for key in ("attention_mask", "cache_position"):
            value = bound.arguments.get(key)
            if isinstance(value, torch.Tensor):
                bound.arguments[key] = value.cpu()
        return original_mask(*bound.args, **bound.kwargs).to(target)
    mask_class._prepare_4d_causal_attention_mask_with_cache_position = staticmethod(cpu_mask)
    original_autocast = torch.autocast
    def compatible_autocast(device_type, *values, **kwargs):
        if device_type == "privateuseone" and kwargs.get("enabled") is False:
            return contextlib.nullcontext()
        return original_autocast(device_type, *values, **kwargs)
    torch.autocast = compatible_autocast
    def fence(module, inputs, outputs):
        value = outputs[0] if isinstance(outputs, tuple) else outputs
        value.reshape(-1)[0].detach().cpu().item()
    for layer in model.visual.vision_model.encoder.layers:
        layer.register_forward_hook(fence)
    processor = AutoProcessor.from_pretrained(str(folder), trust_remote_code=True, local_files_only=True, use_fast=False)
    model.generation_config.pad_token_id = processor.tokenizer.eos_token_id
    def read(array):
        image = Image.fromarray(np.asarray(array, dtype=np.uint8))
        messages = [dict(role="user", content=[dict(type="image", image=image),dict(type="text",text="OCR:")])]
        text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = processor(text=[text], images=[image], return_tensors="pt")
        inputs = {k:v.to(device=device, dtype=torch.float16 if v.is_floating_point() else v.dtype)
            if isinstance(v, torch.Tensor) else v for k,v in inputs.items()}
        with torch.no_grad():
            generated = model.generate(**inputs, max_new_tokens=160, do_sample=False, use_cache=True)
        return processor.batch_decode(generated[:,inputs["input_ids"].shape[1]:].cpu(), skip_special_tokens=True)[0]
    return read, dict(provider="DirectML", adapter=torch_directml.device_name(index).rstrip("\0"))


def main():
    output = sys.stdout.buffer
    selected = sys.argv[1]
    try:
        with contextlib.redirect_stdout(sys.stderr):
            started = time.perf_counter()
            reader, info = {"nar":nar_reader, "paddle":paddle_reader}[selected]()
        write_packet(output, dict(ready=True, model=selected, init_seconds=time.perf_counter()-started, **info))
        while True:
            metadata, arrays = read_packet(sys.stdin.buffer)
            if metadata.get("command") == "close":
                return
            if metadata.get("command") != "recognize" or not 0 < len(arrays) <= 8:
                raise ValueError("InvalidOCRPacket")
            with contextlib.redirect_stdout(sys.stderr):
                texts = []
                for array in arrays:
                    if array.ndim != 3 or array.shape[2] != 3 or min(array.shape[:2]) < 1:
                        raise ValueError("InvalidOCRImage")
                    texts.append(reader(array))
            write_packet(output, dict(texts=texts))
    except (EOFError, BrokenPipeError):
        pass
    except Exception as error:
        write_packet(output, dict(error=type(error).__name__))


if __name__ == "__main__":
    main()
