from abc import ABC, abstractmethod
from typing import Optional
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel

import os
from utils.config_handler import model_conf
from dotenv import load_dotenv
load_dotenv()


class BaseModelFactory(ABC):
    @abstractmethod
    def generator(self) -> Optional[BaseChatModel]:
        pass


# 通义原生协议备选写法（需另外安装 langchain-tongyi，当前未接入依赖）：
# class ChatModelFactory(BaseModelFactory):
#     def generator(self) -> Optional[BaseChatModel]:
#         return init_chat_model(model=model_conf["chat_model_name"], model_provider="tongyi")


class ChatModelFactory(BaseModelFactory):
    def generator(self, model_name: Optional[str] = None) -> Optional[BaseChatModel]:
        model_name = model_name or model_conf["chat_model_name"]
        openai_api_key = os.getenv("OPEN_API_KEY")
        openai_api_base = os.getenv("BASE_URL_DASHSCOPE")
        # LangChain 1.x 统一入口：内部仍是官方维护的 langchain_openai.ChatOpenAI
        return init_chat_model(
            model=model_name,
            model_provider="openai",
            api_key=openai_api_key,
            base_url=openai_api_base,
        )


chat_model = ChatModelFactory().generator()

if __name__ == '__main__':
    print(model_conf["chat_model_name"])
    print(model_conf["backup_chat_model_name"])