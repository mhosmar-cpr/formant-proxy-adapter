import time
import json
import requests
import threading
import asyncio
import websockets
import queue
import socketio

websocket_queues = {}
socketio_queues = {}


def _decode(o):
    # Note the "unicode" part is only for python2
    if isinstance(o, str):
        try:
            return int(o)
        except ValueError:
            return o
    elif isinstance(o, dict):
        return {k: _decode(v) for k, v in o.items()}
    elif isinstance(o, list):
        return [_decode(v) for v in o]
    else:
        return o


class WebSocketSession(threading.Thread):
    def __init__(self, id, fclient, url, queue):
        self.__id = id
        self.__fclient = fclient
        self.__url = url
        self.__queue = queue
        self.__websocket = None
        threading.Thread.__init__(self)

    def run(self):
        async def forward_messages():
            print("starting forwarder")
            while True:
                try:
                    msg = self.__queue.get(block=False)
                except queue.Empty:
                    pass
                else:
                    if self.__websocket is not None:
                        if msg["signal"] == "close":
                            print("closing websocket for " + self.__id)
                            await self.__websocket.close()
                            websocket_queues[self.__id] = None
                            return
                        elif msg["signal"] == "message":
                            await self.__websocket.send(json.dumps(msg["data"]))
                        else:
                            websocket_queues[self.__id] = None
                            print("unknown message")
                            print(msg)
                            raise Exception("unknown message")
                await asyncio.sleep(0)

        async def listen_messages():
            print(self.__id + " connecting to websocket proxy " + str(self.__url))
            async with websockets.connect(self.__url) as websocket:
                self.__websocket = websocket
                print(self.__id + " websocket connected")
                self.__fclient.send_on_custom_data_channel(
                    CHANNEL_NAME,
                    json.dumps({"id": self.__id, "proxy_type": "ws", "event": "open"}).encode(
                        "utf-8"
                    ),
                )
                try:
                    while True:
                        msg = await websocket.recv()
                        self.__fclient.send_on_custom_data_channel(
                            CHANNEL_NAME,
                            json.dumps(
                                {
                                    "id": self.__id,
                                    "proxy_type": "ws",
                                    "event": "message",
                                    "contents": msg,
                                }
                            ).encode("utf-8"),
                        )
                except websockets.ConnectionClosed:
                    self.__websocket = None
                    print(self.__id + " websocket closed")
                self.__fclient.send_on_custom_data_channel(
                    CHANNEL_NAME,
                    json.dumps({"id": self.__id, "proxy_type": "ws", "event": "close"}).encode(
                        "utf-8"
                    ),
                )
                websocket_queues[self.__id] = None

        async def start():
            print("starting websocket proxy for " + self.__id)
            await asyncio.gather(forward_messages(), listen_messages())
            print("ending websocket proxy for " + self.__id)

        asyncio.run(start())


class SocketioSession(threading.Thread):
    def __init__(self, id, fclient, url, namespace, queue):
        self.__id = id
        self.__fclient = fclient
        self.__url = url
        self.__queue = queue
        self.__sio = socketio.AsyncClient()
        self.__namespace = namespace
        threading.Thread.__init__(self)

    def run(self):
        async def forward_messages():
            print("starting forwarder")
            while True:
                try:
                    msg = self.__queue.get(block=False)
                except queue.Empty:
                    pass
                else:
                    if self.__sio is not None:
                        if msg["signal"] == "disconnect":
                            print("closing socketio for " + self.__id)
                            await self.__sio.disconnect()
                            socketio_queues[self.__id] = None
                            return
                        elif msg["signal"] == "message":
                            # print("Got SIO message: ")
                            # print(msg)
                            await self.__sio.emit(
                                msg["topic"], json.dumps(msg["data"] if "data" in msg else {}), namespace=self.__namespace
                            )
                        else:
                            socketio_queues[self.__id] = None
                            print("unknown message")
                            print(msg)
                            raise Exception("unknown message")
                await asyncio.sleep(0)

        async def listen_messages():
            @self.__sio.event(namespace=self.__namespace)
            async def connect():
                print(self.__id + " socketio connected")
                self.__fclient.send_on_custom_data_channel(
                    CHANNEL_NAME,
                    json.dumps({"id": self.__id, "proxy_type": "sio", "event": "connect"}).encode(
                        "utf-8"
                    ),
                )

            @self.__sio.event(namespace=self.__namespace)
            async def disconnect():
                print(self.__id + " socketio disconnected")
                self.__fclient.send_on_custom_data_channel(
                    CHANNEL_NAME,
                    json.dumps(
                        {"id": self.__id, "proxy_type": "sio", "event": "disconnect"}
                    ).encode("utf-8"),
                )

            @self.__sio.on("*", namespace=self.__namespace)
            async def handle_message(event, data):
                # print("Got SIO message from BACKEND: ")
                # print(data)
                self.__fclient.send_on_custom_data_channel(
                    CHANNEL_NAME,
                    json.dumps(
                        {
                            "id": self.__id,
                            "proxy_type": "sio",
                            "event": "message",
                            "topic": event,
                            "contents": data,
                        }
                    ).encode("utf-8"),
                )

            print(self.__id + " connecting to socketio proxy " + str(self.__url))
            await self.__sio.connect(self.__url, namespaces=[self.__namespace])

        async def start():
            print("starting socketio proxy for " + self.__id)
            await asyncio.gather(forward_messages(), listen_messages())
            await self.__sio.wait()
            self.__sio = None
            socketio_queues[self.__id] = None
            print("ending socketio proxy for " + self.__id)

        asyncio.run(start())


from formant.sdk.agent.v1 import Client as FormantAgentClient

CHANNEL_NAME = "http_websocket_proxy"


def main():
    fclient = FormantAgentClient("localhost:5501")

    async def callback(message):
        requestData = json.loads(message.payload, object_hook=_decode)
        id = requestData["id"]
        if requestData["proxy_type"] == "ws":
            if requestData["signal"] == "connect":
                q = queue.Queue()
                websocket_queues[id] = q
                WebSocketSession(id, fclient, requestData["url"], q).start()
            else:
                q = websocket_queues[id]
                if q is not None:
                    q.put(requestData)
        if requestData["proxy_type"] == "sio":
            if requestData["signal"] == "connect":
                q = queue.Queue()
                socketio_queues[id] = q
                SocketioSession(
                    id, fclient, requestData["url"], requestData["namespace"], q
                ).start()
            else:
                q = socketio_queues[id]
                if q is not None:
                    q.put(requestData)
        if requestData["proxy_type"] == "http":
            # print("Got http request: ")
            # print(requestData)
            if ("requestInit" in requestData) == False or requestData["requestInit"][
                "method"
            ] == "GET":
                r = requests.get(requestData["requestInfo"])
                fclient.send_on_custom_data_channel(
                    CHANNEL_NAME,
                    json.dumps(
                        {
                            "id": id,
                            "proxy_type": "http",
                            "status_code": r.status_code,
                            "contents": r.text,
                        }
                    ).encode("utf-8"),
                )
            elif ("requestInit" in requestData) == False or requestData["requestInit"][
                "method"
            ] == "DELETE":
                r = requests.delete(requestData["requestInfo"])
                fclient.send_on_custom_data_channel(
                    CHANNEL_NAME,
                    json.dumps(
                        {
                            "id": id,
                            "proxy_type": "http",
                            "status_code": r.status_code,
                            "contents": r.text,
                        }
                    ).encode("utf-8"),
                )
            elif requestData["requestInit"]["method"] == "POST":
                if isinstance(requestData["requestInit"]["body"], str):
                    # print("using data: " + requestData["requestInit"]["body"])
                    r = requests.post(
                        requestData["requestInfo"], data={requestData["requestInit"]["body"]: ""}
                    )
                else:
                    r = requests.post(
                        requestData["requestInfo"], json=requestData["requestInit"]["body"]
                    )
                fclient.send_on_custom_data_channel(
                    CHANNEL_NAME,
                    json.dumps(
                        {
                            "id": id,
                            "proxy_type": "http",
                            "status_code": r.status_code,
                            "contents": r.text,
                        }
                    ).encode("utf-8"),
                )
            elif requestData["requestInit"]["method"] == "PUT":
                r = requests.put(
                    requestData["requestInfo"], json=requestData["requestInit"]["body"]
                )
                fclient.send_on_custom_data_channel(
                    CHANNEL_NAME,
                    json.dumps(
                        {
                            "id": id,
                            "proxy_type": "http",
                            "status_code": r.status_code,
                            "contents": r.text,
                        }
                    ).encode("utf-8"),
                )
            else:
                raise "Unsupported"

    def example_channel_callback(message):
        asyncio.run(callback(message))

    # Listen to data from the custom web application
    fclient.register_custom_data_channel_message_callback(
        example_channel_callback, channel_name_filter=[CHANNEL_NAME]
    )

    while True:
        time.sleep(0.1)


if __name__ == "__main__":
    main()
