import json
import logging
import openai
import os
import requests
import semantic_kernel as sk
import time
import tiktoken
from shared.util import call_gpt_model, get_chat_history_as_messages, get_message, get_secret
from tenacity import retry, wait_random_exponential, stop_after_attempt
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion

# logging level

logging.getLogger('azure').setLevel(logging.WARNING)
LOGLEVEL = os.environ.get('LOGLEVEL', 'INFO').upper()
logging.basicConfig(level=LOGLEVEL)

# Configurations

FUNCTIONS_CONFIGURATION = f"orc/plugins/functions.json"
QUESTION_ANSWERING_PROMPT_FILE = f"orc/prompts/question_answering.functions.prompt"

# AOAI Integration Settings

AZURE_OPENAI_RESOURCE = os.environ.get("AZURE_OPENAI_RESOURCE")
AZURE_OPENAI_ENDPOINT = f"https://{AZURE_OPENAI_RESOURCE}.openai.azure.com"
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION") or "2023-06-01-preview"
AZURE_OPENAI_CHATGPT_DEPLOYMENT = os.environ.get("AZURE_OPENAI_CHATGPT_DEPLOYMENT")
AZURE_OPENAI_CHATGPT_MODEL = os.environ.get("AZURE_OPENAI_CHATGPT_MODEL") # 'gpt-35-turbo-16k', 'gpt-4', 'gpt-4-32k'
AZURE_OPENAI_EMBEDDING_DEPLOYMENT = os.environ.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT") 
AZURE_OPENAI_TEMPERATURE = os.environ.get("AZURE_OPENAI_TEMPERATURE") or "0.17"
AZURE_OPENAI_TOP_P = os.environ.get("AZURE_OPENAI_TOP_P") or "0.27"
AZURE_OPENAI_RESP_MAX_TOKENS = os.environ.get("AZURE_OPENAI_MAX_TOKENS") or "1536"
AZURE_OPENAI_SYSTEM_MESSAGE = os.environ.get("AZURE_OPENAI_SYSTEM_MESSAGE")
AZURE_OPENAI_STREAM = os.environ.get("AZURE_OPENAI_STREAM") or "false"
AZURE_OPENAI_KEY = get_secret('azureOpenAIKey')
SHOULD_STREAM = True if AZURE_OPENAI_STREAM.lower() == "true" else False

@retry(wait=wait_random_exponential(min=20, max=60), stop=stop_after_attempt(12), reraise=True)
def call_semantic_function(function, context):
    semantic_response = function(context = context)
    return semantic_response

@retry(wait=wait_random_exponential(min=2, max=60), stop=stop_after_attempt(12), reraise=True)
def chat_complete(messages, functions, function_call='auto'):
    """  Return assistant chat response based on user query. Assumes existing list of messages """
    
    url = f"{AZURE_OPENAI_ENDPOINT}/openai/deployments/{AZURE_OPENAI_CHATGPT_DEPLOYMENT}/chat/completions?api-version={AZURE_OPENAI_API_VERSION}"

    headers = {
        "Content-Type": "application/json",
        "api-key": AZURE_OPENAI_KEY
    }

    data = {
        "messages": messages,
        "functions": functions,
        "function_call": function_call,
        "temperature" : 0,
    }

    response = requests.post(url, headers=headers, data=json.dumps(data)).json()
    return response

def get_answer(history):

    #############################
    # INITIALIZATION
    #############################

    #initialize variables
    prompt = ""
    answer = ""
    search_query = ""
    prompt_tokens = 0
    completion_tokens = 0
    answer_dict = {}
    rag_processing_error = False

    # initialize kernel
    kernel = sk.Kernel()
    kernel.add_chat_service("chat_completion", AzureChatCompletion(AZURE_OPENAI_CHATGPT_DEPLOYMENT, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_KEY))

    # load openai function definitions
    with open(FUNCTIONS_CONFIGURATION, 'r') as f:
        functions_definitions = json.load(f)

    # load sk rag plugin
    rag_plugin = kernel.import_semantic_skill_from_directory("orc/plugins", "RAG")
    native_functions = kernel.import_native_skill_from_directory("orc/plugins", "RAG")
    rag_plugin.update(native_functions)
    
    # map functions to code
    function_dict = {
        "get_sources": rag_plugin["Retrieval"],
    }

    # prepare messages history
    messages = []
    chat_history_messages = get_chat_history_as_messages(history, include_last_turn=True)
    prompt  = open(QUESTION_ANSWERING_PROMPT_FILE, "r").read()             
    messages = [{"role": "system", "content": prompt}] + chat_history_messages

    #############################
    # RAG FLOW
    #############################

    try:

        # first call to the model to see if a function call is needed
        response = chat_complete(messages, functions=functions_definitions, function_call="auto")
        response_message = response['choices'][0]['message']
        prompt_tokens += response['usage']['prompt_tokens']
        completion_tokens += response['usage']['completion_tokens']

        if 'function_call' in response_message:

            function_name = response_message["function_call"]["name"]
            function_args = json.loads(response_message["function_call"]["arguments"])
            function_to_call = function_dict[function_name] 
            context_variables = sk.ContextVariables()
            context_variables.update(function_args)
            function_response = function_to_call(context_variables.variables)

            # add the function results to the messages history giving context to the model
            messages = messages + [
                {
                    "role": response_message["role"],
                    "function_call": {
                        "name": function_name,
                        "arguments": response_message["function_call"]["arguments"],
                    },
                    "content": None
                },
                {
                    "role": "function",
                    "name": function_name,
                    "content": function_response.result,
                }
            ]                    

            # generates the answer after adding the function results to the context
            response = chat_complete(messages, functions=functions_definitions, function_call="none")
            response_message = response['choices'][0]['message']
            prompt_tokens += response['usage']['prompt_tokens']
            completion_tokens += response['usage']['completion_tokens']
            
            # add the assistant answer
            messages.append( # adding assistant response to messages
                {
                    "role": response_message["role"],
                    "content": response_message["content"]
                }
            )

            # store answer metadata when calling get_sources function
            if function_name == "get_sources":
                answer_dict["search_query"] = function_args['question']                
                answer_dict["sources"] = function_response.result

        else:
            
            # add the assistant answer
            messages.append( # adding assistant response to messages
                {
                    "role": response_message["role"],
                    "content": response_message["content"]
                }
            )  

        # answer generated by the model for the user question
        answer = messages[-1]['content']

    except Exception as e:
        logging.error(f"[code_orchestration] error when executing RAG flow. {e}")
        answer = f"{get_message('ERROR_ANSWER')} RAG flow: {e}"
        rag_processing_error = True

 
    #############################
    # QUALITY CONTROL STEP
    #############################
 
    # groudedness needs to be equal or greater than 3 to be considered a good answer
    next_to_last = messages[-2]
    if next_to_last['role'] == 'function' and next_to_last['name'] == 'get_sources' and not rag_processing_error:
        try:
            # call semantic function to calculate groudedness
            context = kernel.create_new_context()
            context['answer'] =  answer
            context['sources'] = next_to_last['content']
            semantic_response = call_semantic_function(rag_plugin["groudedness"], context)
            if semantic_response.result.isdigit():
                gpt_groudedness = int(semantic_response.result)  
                logging.info(f"[code_orchestration] groudedness: {gpt_groudedness}.")
                if gpt_groudedness < 3: 
                    answer = get_message('UNGROUNDED_ANSWER')
                answer_dict['gpt_groudedness'] = gpt_groudedness
            else:
                logging.error(f"[code_orchestration] could not calculate groudedness.")
        except Exception as e:
            logging.error(f"[code_orchestration] could not calculate groudedness. {e}")

    answer_dict["prompt"] = prompt
    answer_dict["answer"] = answer
    answer_dict["prompt_tokens"] = prompt_tokens
    answer_dict["completion_tokens"] = completion_tokens

    return answer_dict