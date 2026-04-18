import os
from traceback import print_exc
from typing import Callable, Dict, TypedDict
import gradio as gr
import torch
from loguru import logger
from natsort import natsorted
from SVCFusion.const_vars import WORK_DIR_PATH
from SVCFusion.i18n import I
from SVCFusion.model_utils import detect_current_model_by_path
from SVCFusion.models.inited import model_name_list, model_list
from SVCFusion.inference.vocoders import get_vocoder_keys
from SVCFusion.ui.DeviceChooser import DeviceChooser
from SVCFusion.ui.Form import Form
from SVCFusion.ui.FormTypes import FormDict
from SVCFusion.ui.component.Warning import WarningHTML


class ModelDropdownInfo(TypedDict):
    model_type_index: str
    model_type_name: str


class ModelChooser:
    def result_normalize(self, result):
        for key in result:
            if isinstance(result[key], str) and (
                result[key].endswith(I.model_chooser.unuse_value)
                or result[key].endswith(I.model_chooser.no_model_value)
            ):
                result[key] = None
        return result

    # def get_on_topic_result(): ...

    def refresh_search_paths(self):
        self.search_paths = [
            WORK_DIR_PATH,
            *[
                "archive/" + p
                for p in os.listdir("archive")
                if os.path.isdir(os.path.join("archive", p))
            ],
            *[
                "models/" + p
                for p in os.listdir("models")
                if os.path.isdir(os.path.join("models", p))
            ],
        ]
        self.choices = self.search_path_to_text()

    def search_path_to_text(self):
        return [
            I.model_chooser.workdir_name,
            *[
                p.replace("models/", f"{I.model_chooser.models_dir_name} - ").replace(
                    "archive/", f"{I.model_chooser.archive_dir_name} - "
                )
                for p in self.search_paths
                if not p.startswith("exp")
            ],
        ]

    def get_search_path_val(self):
        self.refresh_search_paths()
        return gr.update(
            choices=self.choices,
            value=self.choices[0],
        )

    def get_models_from_search_path(self, search_path):
        # os 扫描目录 获取模型
        model_type_index = detect_current_model_by_path(search_path, alert=True)
        result = {}
        for p in os.listdir(search_path):
            if os.path.isfile(os.path.join(search_path, p)):
                model_type = model_list[model_type_index].model_filter(p)
                if result.get(model_type) is None:
                    result[model_type] = []
                result[model_type].append(p)
        if os.path.exists(search_path + "/diffusion") and os.path.isdir(
            search_path + "/diffusion"
        ):
            for p in os.listdir(search_path + "/diffusion"):
                if os.path.isfile(os.path.join(search_path + "/diffusion", p)):
                    model_type = model_list[model_type_index].model_filter(
                        search_path + "/diffusion/" + p
                    )
                    if result.get(model_type) is None:
                        result[model_type] = []
                    result[model_type].append("diffusion/" + p)

        result["vocoder"] = get_vocoder_keys()

        for model_type in result:
            result[model_type] = natsorted(result[model_type])

        return result

    selected_search_path = ""

    search_path_handlers = []

    def on_search_path_change(self, handler):
        self.search_path_handlers.append(handler)

    def update_search_path(self, search_path):
        self.selected_search_path = search_path
        for handler in self.search_path_handlers:
            handler(search_path)

    def update_selected(self, search_path, device, *params_values):
        search_path = self.search_paths[search_path]
        model_type_index = detect_current_model_by_path(search_path)

        model_dropdown_values = params_values[: len(self.dropdown_index2model_info)]
        extra_form_values = params_values[len(self.dropdown_index2model_info) :]

        result = {}
        on_topic_extra_form_values = {}

        for i in range(len(extra_form_values)):
            info = self.extra_form.index2param_info[i]
            if info["model_name"] == model_name_list[model_type_index]:
                on_topic_extra_form_values[info["key"]] = extra_form_values[i]

        device = DeviceChooser.get_device_str_from_index(device)

        for i in range(len(model_dropdown_values)):
            model_dropdown = model_dropdown_values[i]
            model_info = self.dropdown_index2model_info[i]
            if model_info["model_type_index"] == model_type_index:
                # print(model_info["model_type_name"], model_dropdown)
                if model_info["model_type_name"] == "vocoder":
                    result["vocoder"] = model_dropdown
                else:
                    result[model_info["model_type_name"]] = os.path.join(
                        search_path, model_dropdown
                    )
            i += 1
        result["device"] = device
        result.update(on_topic_extra_form_values)

        result = self.result_normalize(result)
        self.selected_parameters = result
        self.seleted_model_type_index = model_type_index

    def on_submit(self):
        try:
            spks = self.submit_func(
                self.seleted_model_type_index, self.selected_parameters
            )
        except Exception as e:
            logger.error(f"Error during load model: {e}")
            print_exc()
            spks = []
        if spks is None:
            spks = []
        return (
            gr.update(
                choices=spks if len(spks) > 0 else [I.model_chooser.no_spk_value],
                value=spks[0] if len(spks) > 0 else I.model_chooser.no_spk_value,
            ),
            gr.update(visible=False if len(spks) > 0 else True),
        )

    def api_load_model_by_path(self, model_dir_path):
        try:
            if not model_dir_path or not os.path.isdir(model_dir_path):
                return f"ERROR: Path not found or not directory: {model_dir_path}", []

            model_type_index = detect_current_model_by_path(model_dir_path, alert=True)
            if model_type_index < 0 or model_type_index >= len(model_list):
                return f"ERROR: Unknown model type (index={model_type_index}) for path: {model_dir_path}", []

            target_model = model_list[model_type_index]
            model_type_names = list(target_model.model_types.keys())

            pt_files = []
            for f in os.listdir(model_dir_path):
                fp = os.path.join(model_dir_path, f)
                if os.path.isfile(fp):
                    filtered = target_model.model_filter(f)
                    if filtered and filtered in model_type_names:
                        pt_files.append((filtered, fp))

            diffusion_dir = os.path.join(model_dir_path, "diffusion")
            if os.path.isdir(diffusion_dir):
                for f in os.listdir(diffusion_dir):
                    fp = os.path.join(diffusion_dir, f)
                    if os.path.isfile(fp):
                        filtered = target_model.model_filter(fp)
                        if filtered and filtered in model_type_names:
                            pt_files.append((filtered, fp))

            if not pt_files:
                return f"ERROR: No .pt model file found in {model_dir_path}", []

            device = "cuda" if torch.cuda.is_available() else "cpu"

            params = {"device": device}
            for type_name, file_path in pt_files:
                params[type_name] = file_path

            vocoder_keys = get_vocoder_keys()
            if vocoder_keys:
                params["vocoder"] = vocoder_keys[0]

            if hasattr(target_model, "model_chooser_extra_form"):
                extra = target_model.model_chooser_extra_form
                for key in extra:
                    item = extra[key]
                    default = item.get("default", None)
                    if default is not None:
                        params[key] = default() if callable(default) else default

            print(f"[api_load_model_by_path] DIRECT load: {model_dir_path}, type={target_model.model_name}, type_idx={model_type_index}")
            print(f"[api_load_model_by_path] Params keys: {list(params.keys())}")
            for k, v in params.items():
                if k != "device":
                    print(f"  {k} = {v}")

            from SVCFusion.inference.vocoders import set_shared_vocoder
            set_shared_vocoder(params["vocoder"], params["device"])
            print(f"[api_load_model_by_path] Vocoder set: {params['vocoder']}")

            spks = target_model.load_model(params)
            if spks is None:
                spks = []
            print(f"[api_load_model_by_path] load_model returned spks: {spks}")

            self.selected_parameters = params
            self.seleted_model_type_index = model_type_index

            model_name = model_name_list[model_type_index] if model_type_index < len(model_name_list) else "Unknown"
            msg = f"OK: Loaded {model_name} from {os.path.basename(model_dir_path)}, speakers={spks}"
            print(f"[api_load_model_by_path] {msg}")
            return msg, spks
        except Exception as e:
            logger.error(f"api_load_model_by_path error: {e}")
            print_exc()
            return f"ERROR: {e}", []

    def on_refresh(self, search_path):
        search_path = self.search_paths[search_path]
        self.update_search_path(search_path)

        models = self.get_models_from_search_path(search_path)
        model_type_index = detect_current_model_by_path(search_path)

        result = []
        i = 0
        # 遍历所有模型类型，为每个下拉框生成更新配置
        for model in model_list:
            for type in model.model_types:
                # 获取当前模型类型的可用模型列表
                m: list = models.get(type, [I.model_chooser.no_model_value])

                # 为非vocoder模型添加"不使用"选项，vocoder模型必须选择
                if type != "vocoder" and I.model_chooser.unuse_value not in m:
                    m.append(I.model_chooser.unuse_value)

                # 创建下拉框更新配置
                result.append(
                    gr.update(
                        choices=m,  # 设置选项列表
                        value=m[0]
                        if len(m) > 0
                        else I.model_chooser.no_model_value,  # 设置默认值
                        visible=self.dropdown_index2model_info[i]["model_type_index"]
                        == model_type_index,  # 根据模型类型控制显示/隐藏
                    )
                )
                i += 1
        # print(len(result))
        return (
            *result,
            gr.update(
                value=model_name_list[model_type_index],
                interactive=model_type_index == -1,
            ),
            gr.update(visible=model_type_index != -1 and self.show_submit_button),
        )

    def on_refresh_with_search_path(self, search_path):
        if not search_path:
            result = [gr.update() for i in range((len(self.model_dropdowns) + 3))]
            return result
        result = self.on_refresh(search_path)
        return (
            gr.update(
                choices=self.choices,
                value=search_path,
            ),
            *result,
        )

    def __init__(
        self,
        on_submit: Callable = lambda *x: None,
        show_options=True,
        show_submit_button=True,
        submit_btn_text=I.model_chooser.submit_btn_value,
        show_unloaded_tip=True,
    ) -> None:
        self.submit_func = on_submit
        self.show_submit_button = show_submit_button

        self.search_paths = self.refresh_search_paths()
        self.seach_path_dropdown = gr.Dropdown(
            label=I.model_chooser.search_path_label,
            value=self.get_search_path_val,
            type="index",
            interactive=True,
            allow_custom_value=True,
        )
        models = self.get_models_from_search_path(self.search_paths[0])
        self.model_dropdowns = []
        self.dropdown_index2model_info: Dict[int, ModelDropdownInfo] = {}
        is_first_model = True
        i = 0
        extra_form = {}

        for model in model_list:
            for type in model.model_types:
                m = models.get(type, [I.model_chooser.no_model_value])
                self.model_dropdowns.append(
                    gr.Dropdown(
                        label=f"{I.model_chooser.choose_model_dropdown_prefix} - "
                        + model.model_types[type],
                        choices=m,
                        value=m[0] if len(m) > 0 else I.model_chooser.no_model_value,
                        interactive=True,
                        visible=is_first_model,
                    )
                )
                self.dropdown_index2model_info[i] = {
                    "model_type_index": model_name_list.index(model.model_name),
                    "model_type_name": type,
                }
                is_first_model = False
                i += 1

            if hasattr(model, "model_chooser_extra_form"):
                model_chooser_extra_form: FormDict = model.model_chooser_extra_form
                extra_form[model.model_name] = {
                    "form": model_chooser_extra_form,
                    "callback": lambda: None,
                }

        with gr.Group():
            self.refresh_btn = gr.Button(
                I.model_chooser.refresh_btn_value,
                interactive=True,
            )
        with gr.Group():
            with gr.Row():
                self.model_type_dropdown = gr.Dropdown(
                    label=I.model_chooser.model_type_dropdown_label,
                    choices=model_name_list,
                    interactive=False,
                )
                self.device_chooser = DeviceChooser(show=show_options)

            self.spk_dropdown = gr.Dropdown(
                label=I.model_chooser.spk_dropdown_label,
                choices=[I.model_chooser.no_spk_option],
                value=I.model_chooser.no_spk_option,
                interactive=True,
                visible=show_options,
            )
        if len(extra_form) > 0 and show_options:
            self.extra_form = Form(
                triger_comp=self.model_type_dropdown,
                models=extra_form,
                show_submit=False,
                vertical=True,
            )

        self.unloaded_tip = gr.HTML(
            WarningHTML(I.main_ui.unloaded_model_tip), visible=show_unloaded_tip
        )

        self.load_model_btn = gr.Button(
            submit_btn_text,
            variant="primary",
            interactive=True,
            visible=show_submit_button,
        )

        self.seach_path_dropdown.change(
            self.on_refresh,
            [self.seach_path_dropdown],
            [
                *self.model_dropdowns,
                self.model_type_dropdown,
                self.load_model_btn,
            ],
            api_name="on_refresh",
        )

        self.refresh_btn.click(
            self.on_refresh,
            [self.seach_path_dropdown],
            [
                *self.model_dropdowns,
                self.model_type_dropdown,
                self.load_model_btn,
            ],
            api_name="on_refresh",
        )

        self.load_model_btn.click(
            self.on_submit,
            outputs=[self.spk_dropdown, self.unloaded_tip],
            api_name="on_submit",
        )

        if self.show_submit_button:
            self._api_load_path_input = gr.Textbox(
                label="API: Load model by path (internal)",
                visible=False,
            )
            self._api_load_result = gr.Textbox(
                label="API: Load result",
                visible=False,
            )
            self._api_load_speakers = gr.Dropdown(
                label="API: Loaded speakers",
                visible=False,
            )
            self._api_load_btn = gr.Button(
                "API: Load model by path",
                visible=False,
            )
            self._api_load_btn.click(
                self.api_load_model_by_path,
                inputs=[self._api_load_path_input],
                outputs=[self._api_load_result, self._api_load_speakers],
                api_name="api_load_model_by_path",
            )

        for item in [
            self.seach_path_dropdown,
            self.model_type_dropdown,
            *self.model_dropdowns,
            *(self.extra_form.param_comp_list if hasattr(self, "extra_form") else []),
        ]:
            item.change(
                self.update_selected,
                inputs=[
                    self.seach_path_dropdown,
                    self.device_chooser.device_dropdown,
                    *self.model_dropdowns,
                    *(
                        self.extra_form.param_comp_list
                        if hasattr(self, "extra_form")
                        else []
                    ),
                ],
            )
