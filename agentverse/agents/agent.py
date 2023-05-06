import logging
import os
from typing import Any, List, Optional, Sequence, Tuple, Union

from langchain.agents import AgentExecutor, AgentOutputParser
from langchain.agents.agent import Agent as langchainAgent
from langchain.agents.agent import AgentOutputParser
from langchain.agents.conversational_chat.output_parser import ConvoOutputParser
from langchain.base_language import BaseLanguageModel
from langchain.callbacks.base import BaseCallbackManager
from langchain.chains import LLMChain
from langchain.chat_models.base import BaseChatModel
from langchain.llms.base import BaseLLM
from langchain.memory import ChatMessageHistory
from langchain.prompts import BasePromptTemplate, PromptTemplate
from langchain.prompts.chat import ChatPromptTemplate, MessagesPlaceholder
from langchain.schema import (AgentAction, AIMessage,
                              BaseMessage, HumanMessage, SystemMessage)
from langchain.tools.base import BaseTool

from agentverse.memory import SummaryMemory
from agentverse.message import Message
from agentverse.parser import OutputParseError

try:
    import openai
except ImportError:
    is_openai_available = False
    logging.warning("openai package is not installed")
else:
    openai.api_key = os.environ.get("OPENAI_API_KEY")
    openai.proxy = os.environ.get("http_proxy")
    if openai.proxy is None:
        openai.proxy = os.environ.get("HTTP_PROXY")
    if openai.api_key is None:
        logging.warning("OpenAI API key is not set. Please set the environment variable OPENAI_API_KEY")
        is_openai_available = False
    else:
        is_openai_available = True


class Agent(langchainAgent):
    """Action unit in the environment.
    
    Args:
        name: Name of the agent
        role_description: Description of the role of the agent
        memory: Memory of the agent. This is used to store the chat history.
        max_retry: Maximum number of retries when encountering an error during generation
        tool_memory: Memory of the tools. This is used to store the observations from executing tools by this agent.
        tools: List of tools that the agent can use
        receiver: List of receivers of the messages generated by this agent
    """
    name: str
    role_description: str
    memory: ChatMessageHistory
    max_retry: int = 3
    tool_memory: Optional[SummaryMemory] = None
    tools: List[BaseTool] = []
    receiver: List[str] = ["all"]
    input_variables: List[str] = ["chat_history", "agent_scratchpad"]

    @classmethod
    def _get_default_output_parser(cls, **kwargs: Any) -> AgentOutputParser:
        return ConvoOutputParser()

    @property
    def _agent_type(self) -> str:
        raise NotImplementedError

    @property
    def observation_prefix(self) -> str:
        """Prefix to append the observation with."""
        return "Observation: "

    @property
    def llm_prefix(self) -> str:
        """Prefix to append the llm call with."""
        return ""

    def get_receiver(self) -> List[str]:
        return self.receiver

    def set_receiver(self, receiver: List[str]) -> None:
        self.receiver = receiver

    def step(self, env_description: str="") -> Message:
        """Generate the next message"""
        executor = AgentExecutor.from_agent_and_tools(
            agent=self,
            tools=self.tools,
            verbose=True,
            return_intermediate_steps=True,
            output_parser=self.output_parser
        )
        input_arguments = {"agent_name": self.name}
        if isinstance(self.llm_chain.llm, BaseChatModel):
            chat_history = self.memory.messages
        elif isinstance(self.llm_chain.llm, BaseLLM):
            messages = self.memory.messages
            chat_history = "\n".join([message.content for message in messages])
            chat_history += f"\n{self.name}: "
        else:
            raise ValueError(f"Unsupported LLM type: {self.llm_chain.llm}")

        input_arguments['chat_history'] = chat_history
        input_arguments['env_description'] = env_description

        response = None
        for i in range(self.max_retry):
            try:
                response = executor(input_arguments)
                break
            except OutputParseError as e:
                print(e)
                print("Retrying...")
                continue
        if response is None:
            raise ValueError(f"{self.name} failed to generate valid response.")

        message = Message(
            content=response['output'],
            role="assistant",
            sender=self.name,
            receiver=self.get_receiver(),
            tool_response=response['intermediate_steps']
        )
        return message

    async def astep(self, env_description: str="") -> Message:
        """Asynchronous version of step"""
        executor = AgentExecutor.from_agent_and_tools(
            agent=self,
            tools=self.tools,
            verbose=True,
            return_intermediate_steps=True,
            output_parser=self.output_parser
        )
        input_arguments = {"agent_name": self.name}
        if isinstance(self.llm_chain.llm, BaseChatModel):
            chat_history = self.memory.messages
        elif isinstance(self.llm_chain.llm, BaseLLM):
            messages = self.memory.messages
            chat_history = "\n".join([message.content for message in messages])
            chat_history += f"\n{self.name}: "
        else:
            raise ValueError(f"Unsupported LLM type: {self.llm_chain.llm}")
        if self.tool_memory is not None:
            input_arguments['tool_memory'] = self.tool_memory.buffer

        input_arguments['chat_history'] = chat_history
        input_arguments['env_description'] = env_description
        if self.tool_memory is not None:
            input_arguments['tool_memory'] = self.tool_memory.buffer
        else:
            input_arguments['tool_memory'] = ""

        response = None
        for i in range(self.max_retry):
            try:
                response = await executor.acall(input_arguments)
                break
            except OutputParseError as e:
                print(e)
                print("Retrying...")
                continue
        if response is None:
            logging.error(f"{self.name} failed to generate valid response.")

        message = Message(
            content="" if response is None else response['output'],
            role="assistant",
            sender=self.name,
            receiver=self.get_receiver(),
            tool_response=[] if response is None else response['intermediate_steps']
        )
        return message

    @classmethod
    def create_prompt(
        cls,
        llm: BaseLanguageModel,
        tools: List[BaseTool],
        prefix_prompt: str = "",
        format_prompt: str = "",
        suffix_prompt: str = "",
        input_variables: Optional[List[str]] = None,
    ) -> BasePromptTemplate:
        """Create the prompt for the agent.

        Args:
            llm: Language model to use. Following the interface of langchain.llms
            tools: List of tools to use. Following the interface of langchain.tools
            prefix_prompt: Introduce the scene
            format_prompt: Specifying the format of the output and the rule of the environment
            suffix_prompt: Telling the agent to response
            input_variables: List of placeholder variable names in your prompt
        """
        if input_variables is None:
            if len(tools) > 0:
                input_variables = ["chat_history", "agent_scratchpad"]
            else:
                input_variables = ["chat_history"]
        if isinstance(llm, BaseChatModel):
            messages = [
                SystemMessage(content=prefix_prompt),
                SystemMessage(content=format_prompt),
                SystemMessage(content=suffix_prompt),
                MessagesPlaceholder(variable_name="chat_history")
            ]
            if len(tools) > 0:
                messages.append(MessagesPlaceholder(variable_name="agent_scratchpad"))
            return ChatPromptTemplate(input_variables=input_variables, messages=messages)
        elif isinstance(llm, BaseLLM):
            template = f"{prefix_prompt}\n\n{format_prompt}\n\n{suffix_prompt}"
            return PromptTemplate(input_variables=input_variables, template=template)
        else:
            raise ValueError(f"Unsupported LLM type: {llm}")

    def _construct_scratchpad(
        self, intermediate_steps: List[Tuple[AgentAction, str]]
    ) -> Union[List[BaseMessage], str]:
        """Construct the scratchpad that lets the agent continue its thought process."""
        thoughts = [] if isinstance(self.llm_chain.llm, BaseChatModel) else ""
        for action, observation in intermediate_steps:
            if isinstance(self.llm_chain.llm, BaseChatModel):
                thoughts.append(AIMessage(content=action.log.strip()))
                human_message = HumanMessage(
                    content="Tool response:\n{observation}".format(observation=observation)
                )
                thoughts.append(human_message)
            elif isinstance(self.llm_chain.llm, BaseLLM):
                thoughts += action.log.strip()
                thoughts += "\n" + "Observation: " + observation
        return thoughts

    @classmethod
    def from_llm_and_tools(
        cls,
        llm: BaseLanguageModel,
        tools: Sequence[BaseTool],
        callback_manager: Optional[BaseCallbackManager] = None,
        output_parser: Optional[AgentOutputParser] = None,
        prefix_prompt: str = "",
        format_prompt: str = "",
        suffix_prompt: str = "",
        input_variables: Optional[List[str]] = None,
        **kwargs
    ) -> langchainAgent:
        """Construct an agent from an LLM and tools."""
        cls._validate_tools(tools)
        prompt = cls.create_prompt(
            llm=llm,
            tools=tools,
            prefix_prompt=prefix_prompt,
            format_prompt=format_prompt,
            suffix_prompt=suffix_prompt,
            input_variables=input_variables,
        )
        llm_chain = LLMChain(
            llm=llm,
            prompt=prompt,
            callback_manager=callback_manager,
        )
        tool_names = [tool.name for tool in tools]
        return cls(
            llm_chain=llm_chain,
            tools=tools,
            allowed_tools=tool_names,
            output_parser=output_parser,
            **kwargs,
        )

    def reset(self):
        self.memory.clear()