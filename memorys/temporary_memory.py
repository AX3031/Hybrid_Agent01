
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnableWithMessageHistory
from langchain_core.chat_history import InMemoryChatMessageHistory
# 后续可考虑用InMemorySaver

from model.factory import chat_model
from langchain_core.prompts import PromptTemplate, ChatPromptTemplate,ChatMessagePromptTemplate, MessagesPlaceholder

from utils.prompt_loader import load_rag_prompts



model = chat_model
# prompt_template = PromptTemplate.from_template(load_rag_prompts())

# prompt_template = PromptTemplate.from_template(
#     "你需要根据会话历史回应用户问题，对话历史：{chat_history}，用户提问：{input}，请回答"
# )

prompt_template = ChatPromptTemplate.from_messages(
    [
        ("system","你需要根据会话历史回应用户问题，对话历史："),
        MessagesPlaceholder("chat_history"),
        ("human","请回答如下问题,{input}")
    ]
)
str_parser = StrOutputParser()


def print_prompt(full_prompt):
    print("="*20,full_prompt.to_string(), "="*20)
    return full_prompt



base_chain = prompt_template | print_prompt | model | str_parser

store = {}          # key就是session, value就是InMemoryChatMessageHistory类对象
# 实现通过会话id获取InMemoryChatMessageHistory类对象
def get_history(session_id):
    if session_id not in store:
        store[session_id] = InMemoryChatMessageHistory()

    return store[session_id]

# 创建一个新的链，对原有链增强功能：自动附加历史消息
conversation_chain = RunnableWithMessageHistory(
    base_chain,         # 被增强的原有chain
    get_history,            # 通过会话id获取InMemoryChatMessageHistory类对象
    input_messages_key="input",         # 表示用户输入在模板中的占位符
    history_messages_key="chat_history"         # 表示用户输入历史消息在模板中的占位符
)


if __name__ == '__main__':
    # 固定格式，添加LangChain的配置，为当前程序配置所属的session_id
    session_config = {
        "configurable":{
            "session_id": "user_001"
        }
    }

    res = conversation_chain.invoke({"input": "小明有2只猫"},session_config)
    print("第1次执行",res)

    res = conversation_chain.invoke({"input": "小刚有1只狗"},session_config)
    print("第2次执行",res)

    res = conversation_chain.invoke({"input": "总共有几个宠物"},session_config)
    print("第3次执行",res)
