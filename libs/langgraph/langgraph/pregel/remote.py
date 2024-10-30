from typing import (
    Any,
    AsyncIterator,
    Iterator,
    Literal,
    Optional,
    Sequence,
    Union,
    cast,
)

import orjson
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.graph import (
    Edge as DrawableEdge,
)
from langchain_core.runnables.graph import (
    Graph as DrawableGraph,
)
from langchain_core.runnables.graph import (
    Node as DrawableNode,
)
from langgraph_sdk.client import (
    LangGraphClient,
    SyncLangGraphClient,
    get_client,
    get_sync_client,
)
from langgraph_sdk.schema import Checkpoint, ThreadState
from langgraph_sdk.schema import StreamMode as StreamModeSDK
from typing_extensions import Self

from langgraph.checkpoint.base import CheckpointMetadata
from langgraph.constants import (
    CONF,
    CONFIG_KEY_CHECKPOINT_NS,
    CONFIG_KEY_STREAM,
    INTERRUPT,
)
from langgraph.errors import GraphInterrupt
from langgraph.pregel.protocol import PregelProtocol
from langgraph.pregel.types import All, PregelTask, StateSnapshot, StreamMode
from langgraph.types import Interrupt, StreamProtocol
from langgraph.utils.config import merge_configs


class RemoteException(Exception):
    """Exception raised when an error occurs in the remote graph."""

    pass


class RemoteGraph(PregelProtocol):
    """The `RemoteGraph` class is a client implementation for calling remote
    APIs that implement the LangGraph Server API specification.

    For example, the `RemoteGraph` class can be used to call APIs from deployments
    on LangGraph Cloud.

    `RemoteGraph` behaves the same way as a `Graph` and can be used directly as
    a node in another `Graph`.
    """

    name: str

    def __init__(
        self,
        name: str,  # graph_id
        /,
        *,
        url: Optional[str] = None,
        api_key: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        client: Optional[LangGraphClient] = None,
        sync_client: Optional[SyncLangGraphClient] = None,
        config: Optional[RunnableConfig] = None,
    ):
        """Specify `url`, `api_key`, and/or `headers` to create default sync and async clients.

        If `client` or `sync_client` are provided, they will be used instead of the default clients.
        See `LangGraphClient` and `SyncLangGraphClient` for details on the default clients. At least
        one of `url`, `client`, or `sync_client` must be provided.

        Args:
            name: The name of the graph.
            url: The URL of the remote API.
            api_key: The API key to use for authentication. If not provided, it will be read from the environment (`LANGGRAPH_API_KEY`, `LANGSMITH_API_KEY`, or `LANGCHAIN_API_KEY`).
            headers: Additional headers to include in the requests.
            client: A `LangGraphClient` instance to use instead of creating a default client.
            sync_client: A `SyncLangGraphClient` instance to use instead of creating a default client.
            config: An optional `RunnableConfig` instance with additional configuration.
        """
        self.name = name
        self.config = config

        if client is None and url is not None:
            client = get_client(url=url, api_key=api_key, headers=headers)
        self.client = client

        if sync_client is None and url is not None:
            sync_client = get_sync_client(url=url, api_key=api_key, headers=headers)
        self.sync_client = sync_client

    def _validate_client(self) -> LangGraphClient:
        if self.client is None:
            raise ValueError(
                "Async client is not initialized: please provide `url` or `client` when initializing `RemoteGraph`."
            )
        return self.client

    def _validate_sync_client(self) -> SyncLangGraphClient:
        if self.sync_client is None:
            raise ValueError(
                "Sync client is not initialized: please provide `url` or `sync_client` when initializing `RemoteGraph`."
            )
        return self.sync_client

    def copy(self, update: dict[str, Any]) -> Self:
        attrs = {**self.__dict__, **update}
        return self.__class__(attrs.pop("name"), **attrs)

    def with_config(
        self, config: Optional[RunnableConfig] = None, **kwargs: Any
    ) -> Self:
        return self.copy(
            {"config": merge_configs(self.config, config, cast(RunnableConfig, kwargs))}
        )

    def _get_drawable_nodes(
        self, graph: dict[str, list[dict[str, Any]]]
    ) -> dict[str, DrawableNode]:
        nodes = {}
        for node in graph["nodes"]:
            node_id = str(node["id"])
            nodes[node_id] = DrawableNode(
                id=node_id,
                name=node.get("name", ""),
                data=node.get("data", {}),
                metadata=node.get("metadata"),
            )
        return nodes

    def get_graph(
        self,
        config: Optional[RunnableConfig] = None,
        *,
        xray: Union[int, bool] = False,
    ) -> DrawableGraph:
        """Get graph by graph name.

        This method calls `GET /assistants/{assistant_id}/graph`.

        Args:
            config: This parameter is not used.
            xray: Include graph representation of subgraphs. If an integer
                value is provided, only subgraphs with a depth less than or
                equal to the value will be included.

        Returns:
            The graph information for the assistant in JSON format.
        """
        sync_client = self._validate_sync_client()
        graph = sync_client.assistants.get_graph(
            assistant_id=self.name,
            xray=xray,
        )
        return DrawableGraph(
            nodes=self._get_drawable_nodes(graph),
            edges=[DrawableEdge(**edge) for edge in graph["edges"]],
        )

    async def aget_graph(
        self,
        config: Optional[RunnableConfig] = None,
        *,
        xray: Union[int, bool] = False,
    ) -> DrawableGraph:
        """Get graph by graph name.

        This method calls `GET /assistants/{assistant_id}/graph`.

        Args:
            config: This parameter is not used.
            xray: Include graph representation of subgraphs. If an integer
                value is provided, only subgraphs with a depth less than or
                equal to the value will be included.

        Returns:
            The graph information for the assistant in JSON format.
        """
        client = self._validate_client()
        graph = await client.assistants.get_graph(
            assistant_id=self.name,
            xray=xray,
        )
        return DrawableGraph(
            nodes=self._get_drawable_nodes(graph),
            edges=[DrawableEdge(**edge) for edge in graph["edges"]],
        )

    def _create_state_snapshot(self, state: ThreadState) -> StateSnapshot:
        tasks = []
        for task in state["tasks"]:
            interrupts = []
            for interrupt in task["interrupts"]:
                interrupts.append(Interrupt(**interrupt))

            tasks.append(
                PregelTask(
                    id=task["id"],
                    name=task["name"],
                    path=tuple(),
                    error=Exception(task["error"]) if task["error"] else None,
                    interrupts=tuple(interrupts),
                    state=self._create_state_snapshot(task["state"])
                    if task["state"]
                    else {"configurable": task["checkpoint"]}
                    if task["checkpoint"]
                    else None,
                    result=task.get("result"),
                )
            )

        return StateSnapshot(
            values=state["values"],
            next=tuple(state["next"]) if state["next"] else tuple(),
            config={
                "configurable": {
                    "thread_id": state["checkpoint"]["thread_id"],
                    "checkpoint_ns": state["checkpoint"]["checkpoint_ns"],
                    "checkpoint_id": state["checkpoint"]["checkpoint_id"],
                    "checkpoint_map": state["checkpoint"].get("checkpoint_map", {}),
                }
            },
            metadata=CheckpointMetadata(**state["metadata"]),
            created_at=state["created_at"],
            parent_config={
                "configurable": {
                    "thread_id": state["parent_checkpoint"]["thread_id"],
                    "checkpoint_ns": state["parent_checkpoint"]["checkpoint_ns"],
                    "checkpoint_id": state["parent_checkpoint"]["checkpoint_id"],
                    "checkpoint_map": state["parent_checkpoint"].get(
                        "checkpoint_map", {}
                    ),
                }
            }
            if state["parent_checkpoint"]
            else None,
            tasks=tuple(tasks),
        )

    def _get_checkpoint(self, config: Optional[RunnableConfig]) -> Optional[Checkpoint]:
        if config is None:
            return None

        checkpoint = {}

        if "thread_id" in config["configurable"]:
            checkpoint["thread_id"] = config["configurable"]["thread_id"]
        if "checkpoint_ns" in config["configurable"]:
            checkpoint["checkpoint_ns"] = config["configurable"]["checkpoint_ns"]
        if "checkpoint_id" in config["configurable"]:
            checkpoint["checkpoint_id"] = config["configurable"]["checkpoint_id"]
        if "checkpoint_map" in config["configurable"]:
            checkpoint["checkpoint_map"] = config["configurable"]["checkpoint_map"]

        return checkpoint if checkpoint else None

    def _get_config(self, checkpoint: Checkpoint) -> RunnableConfig:
        return {
            "configurable": {
                "thread_id": checkpoint["thread_id"],
                "checkpoint_ns": checkpoint["checkpoint_ns"],
                "checkpoint_id": checkpoint["checkpoint_id"],
                "checkpoint_map": checkpoint.get("checkpoint_map", {}),
            }
        }

    def _sanitize_config(self, config: RunnableConfig) -> RunnableConfig:
        reserved_configurable_keys = frozenset(
            [
                "callbacks",
                "checkpoint_map",
                "checkpoint_id",
                "checkpoint_ns",
            ]
        )

        def _sanitize_obj(obj: Any) -> Any:
            """Remove non-JSON serializable fields from the given object."""
            if isinstance(obj, dict):
                return {k: _sanitize_obj(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [_sanitize_obj(v) for v in obj]
            else:
                try:
                    orjson.dumps(obj)
                    return obj
                except orjson.JSONEncodeError:
                    return None

        # Remove non-JSON serializable fields from the config.
        config = _sanitize_obj(config)

        # Only include configurable keys that are not reserved and
        # not starting with "__pregel_" prefix.
        new_configurable = {
            k: v
            for k, v in config["configurable"].items()
            if k not in reserved_configurable_keys and not k.startswith("__pregel_")
        }

        return {
            "tags": config.get("tags") or [],
            "metadata": config.get("metadata") or {},
            "configurable": new_configurable,
        }

    def get_state(
        self, config: RunnableConfig, *, subgraphs: bool = False
    ) -> StateSnapshot:
        """Get the state of a thread.

        This method calls `POST /threads/{thread_id}/state/checkpoint` if a
        checkpoint is specified in the config or `GET /threads/{thread_id}/state`
        if no checkpoint is specified.

        Args:
            config: A `RunnableConfig` that includes `thread_id` in the
                `configurable` field.
            subgraphs: Include subgraphs in the state.

        Returns:
            The latest state of the thread.
        """
        sync_client = self._validate_sync_client()
        merged_config = merge_configs(self.config, config)

        state = sync_client.threads.get_state(
            thread_id=merged_config["configurable"]["thread_id"],
            checkpoint=self._get_checkpoint(merged_config),
            subgraphs=subgraphs,
        )
        return self._create_state_snapshot(state)

    async def aget_state(
        self, config: RunnableConfig, *, subgraphs: bool = False
    ) -> StateSnapshot:
        """Get the state of a thread.

        This method calls `POST /threads/{thread_id}/state/checkpoint` if a
        checkpoint is specified in the config or `GET /threads/{thread_id}/state`
        if no checkpoint is specified.

        Args:
            config: A `RunnableConfig` that includes `thread_id` in the
                `configurable` field.
            subgraphs: Include subgraphs in the state.

        Returns:
            The latest state of the thread.
        """
        client = self._validate_client()
        merged_config = merge_configs(self.config, config)

        state = await client.threads.get_state(
            thread_id=merged_config["configurable"]["thread_id"],
            checkpoint=self._get_checkpoint(merged_config),
            subgraphs=subgraphs,
        )
        return self._create_state_snapshot(state)

    def get_state_history(
        self,
        config: RunnableConfig,
        *,
        filter: Optional[dict[str, Any]] = None,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> Iterator[StateSnapshot]:
        """Get the state history of a thread.

        This method calls `POST /threads/{thread_id}/history`.

        Args:
            config: A `RunnableConfig` that includes `thread_id` in the
                `configurable` field.
            filter: Metadata to filter on.
            before: A `RunnableConfig` that includes checkpoint metadata.
            limit: Max number of states to return.

        Returns:
            States of the thread.
        """
        sync_client = self._validate_sync_client()
        merged_config = merge_configs(self.config, config)

        states = sync_client.threads.get_history(
            thread_id=merged_config["configurable"]["thread_id"],
            limit=limit if limit else 10,
            before=self._get_checkpoint(before),
            metadata=filter,
            checkpoint=self._get_checkpoint(merged_config),
        )
        for state in states:
            yield self._create_state_snapshot(state)

    async def aget_state_history(
        self,
        config: RunnableConfig,
        *,
        filter: Optional[dict[str, Any]] = None,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> AsyncIterator[StateSnapshot]:
        """Get the state history of a thread.

        This method calls `POST /threads/{thread_id}/history`.

        Args:
            config: A `RunnableConfig` that includes `thread_id` in the
                `configurable` field.
            filter: Metadata to filter on.
            before: A `RunnableConfig` that includes checkpoint metadata.
            limit: Max number of states to return.

        Returns:
            States of the thread.
        """
        client = self._validate_client()
        merged_config = merge_configs(self.config, config)

        states = await client.threads.get_history(
            thread_id=merged_config["configurable"]["thread_id"],
            limit=limit if limit else 10,
            before=self._get_checkpoint(before),
            metadata=filter,
            checkpoint=self._get_checkpoint(merged_config),
        )
        for state in states:
            yield self._create_state_snapshot(state)

    def update_state(
        self,
        config: RunnableConfig,
        values: Optional[Union[dict[str, Any], Any]],
        as_node: Optional[str] = None,
    ) -> RunnableConfig:
        """Update the state of a thread.

        This method calls `POST /threads/{thread_id}/state`.

        Args:
            config: A `RunnableConfig` that includes `thread_id` in the
                `configurable` field.
            values: Values to update to the state.
            as_node: Update the state as if this node had just executed.

        Returns:
            `RunnableConfig` for the updated thread.
        """
        sync_client = self._validate_sync_client()
        merged_config = merge_configs(self.config, config)

        response: dict = sync_client.threads.update_state(  # type: ignore
            thread_id=merged_config["configurable"]["thread_id"],
            values=values,
            as_node=as_node,
            checkpoint=self._get_checkpoint(merged_config),
        )
        return self._get_config(response["checkpoint"])

    async def aupdate_state(
        self,
        config: RunnableConfig,
        values: Optional[Union[dict[str, Any], Any]],
        as_node: Optional[str] = None,
    ) -> RunnableConfig:
        """Update the state of a thread.

        This method calls `POST /threads/{thread_id}/state`.

        Args:
            config: A `RunnableConfig` that includes `thread_id` in the
                `configurable` field.
            values: Values to update to the state.
            as_node: Update the state as if this node had just executed.

        Returns:
            `RunnableConfig` for the updated thread.
        """
        client = self._validate_client()
        merged_config = merge_configs(self.config, config)

        response: dict = await client.threads.update_state(  # type: ignore
            thread_id=merged_config["configurable"]["thread_id"],
            values=values,
            as_node=as_node,
            checkpoint=self._get_checkpoint(merged_config),
        )
        return self._get_config(response["checkpoint"])

    def _get_stream_modes(
        self,
        stream_mode: Optional[Union[StreamMode, list[StreamMode]]],
        default: StreamMode = "updates",
    ) -> tuple[list[StreamModeSDK], bool, bool]:
        """Return a tuple of the final list of stream modes sent to the
        remote graph and a boolean flag indicating if stream mode 'updates'
        was present in the original list of stream modes.

        'updates' mode is added to the list of stream modes so that interrupts
        can be detected in the remote graph.
        """
        updated_stream_modes: list[StreamModeSDK] = []
        req_updates = False
        req_single = True
        # coerce to list, or add default stream mode
        if stream_mode:
            if isinstance(stream_mode, str):
                updated_stream_modes.append(stream_mode)
            else:
                req_single = False
                updated_stream_modes.extend(stream_mode)
        else:
            updated_stream_modes.append(default)
        # map "messages" to "messages-tuple"
        if "messages" in updated_stream_modes:
            updated_stream_modes.remove("messages")
            updated_stream_modes.append("messages-tuple")
        # add 'updates' mode if not present
        if "updates" in updated_stream_modes:
            req_updates = True
        else:
            updated_stream_modes.append("updates")
        return (updated_stream_modes, req_updates, req_single)

    def stream(
        self,
        input: Union[dict[str, Any], Any],
        config: Optional[RunnableConfig] = None,
        *,
        stream_mode: Optional[Union[StreamMode, list[StreamMode]]] = None,
        interrupt_before: Optional[Union[All, Sequence[str]]] = None,
        interrupt_after: Optional[Union[All, Sequence[str]]] = None,
        subgraphs: bool = False,
    ) -> Iterator[Union[dict[str, Any], Any]]:
        """Create a run and stream the results.

        This method calls `POST /threads/{thread_id}/runs/stream` if a `thread_id`
        is speciffed in the `configurable` field of the config or
        `POST /runs/stream` otherwise.

        Args:
            input: Input to the graph.
            config: A `RunnableConfig` for graph invocation.
            stream_mode: Stream mode(s) to use.
            interrupt_before: Interrupt the graph before these nodes.
            interrupt_after: Interrupt the graph after these nodes.
            subgraphs: Stream from subgraphs.

        Yields:
            The output of the graph.
        """
        sync_client = self._validate_sync_client()
        merged_config = merge_configs(self.config, config)
        sanitized_config = self._sanitize_config(merged_config)
        stream_modes, req_updates, req_single = self._get_stream_modes(stream_mode)
        stream: Optional[StreamProtocol] = (
            (config or {}).get(CONF, {}).get(CONFIG_KEY_STREAM)
        )
        stream_modes_ext: list[StreamModeSDK] = (
            [*stream_modes, *stream.modes] if stream else stream_modes
        )

        for chunk in sync_client.runs.stream(
            thread_id=sanitized_config["configurable"].get("thread_id"),
            assistant_id=self.name,
            input=input,
            config=sanitized_config,
            stream_mode=stream_modes_ext,
            interrupt_before=interrupt_before,
            interrupt_after=interrupt_after,
            stream_subgraphs=subgraphs or stream is not None,
            if_not_exists="create",
        ):
            if "|" in chunk.event:
                mode, ns_ = chunk.event.split("|", 1)
                ns = tuple(ns_.split("|"))
            else:
                mode, ns = chunk.event, ()
            if caller_ns := (config or {}).get(CONF, {}).get(CONFIG_KEY_CHECKPOINT_NS):
                ns = caller_ns + ns
            if stream is not None and chunk.event in stream.modes:
                stream((ns, mode, chunk.data))
            if chunk.event not in stream_modes:
                continue
            if chunk.event.startswith("updates"):
                if isinstance(chunk.data, dict) and INTERRUPT in chunk.data:
                    raise GraphInterrupt(chunk.data[INTERRUPT])
                if not req_updates:
                    continue
            elif chunk.event.startswith("error"):
                raise RemoteException(chunk.data)
            if subgraphs:
                if "|" in chunk.event:
                    mode, ns_ = chunk.event.split("|", 1)
                    ns = tuple(ns_.split("|"))
                else:
                    mode, ns = chunk.event, ()
                if req_single:
                    yield ns, chunk.data
                else:
                    yield ns, mode, chunk.data
            elif req_single:
                yield chunk.data
            else:
                yield chunk

    async def astream(
        self,
        input: Union[dict[str, Any], Any],
        config: Optional[RunnableConfig] = None,
        *,
        stream_mode: Optional[Union[StreamMode, list[StreamMode]]] = None,
        interrupt_before: Optional[Union[All, Sequence[str]]] = None,
        interrupt_after: Optional[Union[All, Sequence[str]]] = None,
        subgraphs: bool = False,
    ) -> AsyncIterator[Union[dict[str, Any], Any]]:
        """Create a run and stream the results.

        This method calls `POST /threads/{thread_id}/runs/stream` if a `thread_id`
        is speciffed in the `configurable` field of the config or
        `POST /runs/stream` otherwise.

        Args:
            input: Input to the graph.
            config: A `RunnableConfig` for graph invocation.
            stream_mode: Stream mode(s) to use.
            interrupt_before: Interrupt the graph before these nodes.
            interrupt_after: Interrupt the graph after these nodes.
            subgraphs: Stream from subgraphs.

        Yields:
            The output of the graph.
        """
        client = self._validate_client()
        merged_config = merge_configs(self.config, config)
        sanitized_config = self._sanitize_config(merged_config)
        stream_modes, req_updates, req_single = self._get_stream_modes(stream_mode)
        stream: Optional[StreamProtocol] = (
            (config or {}).get(CONF, {}).get(CONFIG_KEY_STREAM)
        )
        stream_modes_ext: list[StreamModeSDK] = (
            [*stream_modes, *stream.modes] if stream else stream_modes
        )

        async for chunk in client.runs.stream(
            thread_id=sanitized_config["configurable"].get("thread_id"),
            assistant_id=self.name,
            input=input,
            config=sanitized_config,
            stream_mode=stream_modes_ext,
            interrupt_before=interrupt_before,
            interrupt_after=interrupt_after,
            stream_subgraphs=subgraphs,
            if_not_exists="create",
        ):
            if "|" in chunk.event:
                mode, ns_ = chunk.event.split("|", 1)
                ns = tuple(ns_.split("|"))
            else:
                mode, ns = chunk.event, ()
            if caller_ns := (config or {}).get(CONF, {}).get(CONFIG_KEY_CHECKPOINT_NS):
                ns = caller_ns + ns
            if stream is not None and chunk.event in stream.modes:
                stream((ns, mode, chunk.data))
            if chunk.event not in stream_modes:
                continue
            if chunk.event.startswith("updates"):
                if isinstance(chunk.data, dict) and INTERRUPT in chunk.data:
                    raise GraphInterrupt(chunk.data[INTERRUPT])
                if not req_updates:
                    continue
            elif chunk.event.startswith("error"):
                raise RemoteException(chunk.data)
            if subgraphs:
                if req_single:
                    yield ns, chunk.data
                else:
                    yield ns, mode, chunk.data
            elif req_single:
                yield chunk.data
            else:
                yield chunk

    async def astream_events(
        self,
        input: Any,
        config: Optional[RunnableConfig] = None,
        *,
        version: Literal["v1", "v2"],
        include_names: Optional[Sequence[All]] = None,
        include_types: Optional[Sequence[All]] = None,
        include_tags: Optional[Sequence[All]] = None,
        exclude_names: Optional[Sequence[All]] = None,
        exclude_types: Optional[Sequence[All]] = None,
        exclude_tags: Optional[Sequence[All]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        raise NotImplementedError

    def invoke(
        self,
        input: Union[dict[str, Any], Any],
        config: Optional[RunnableConfig] = None,
        *,
        interrupt_before: Optional[Union[All, Sequence[str]]] = None,
        interrupt_after: Optional[Union[All, Sequence[str]]] = None,
    ) -> Union[dict[str, Any], Any]:
        """Create a run, wait until it finishes and return the final state.

        This method calls `POST /threads/{thread_id}/runs/wait` if a `thread_id`
        is speciffed in the `configurable` field of the config or
        `POST /runs/wait` otherwise.

        Args:
            input: Input to the graph.
            config: A `RunnableConfig` for graph invocation.
            interrupt_before: Interrupt the graph before these nodes.
            interrupt_after: Interrupt the graph after these nodes.

        Returns:
            The output of the graph.
        """
        for chunk in self.stream(
            input,
            config=config,
            interrupt_before=interrupt_before,
            interrupt_after=interrupt_after,
            stream_mode="values",
        ):
            pass
        try:
            return chunk
        except UnboundLocalError:
            return None

    async def ainvoke(
        self,
        input: Union[dict[str, Any], Any],
        config: Optional[RunnableConfig] = None,
        *,
        interrupt_before: Optional[Union[All, Sequence[str]]] = None,
        interrupt_after: Optional[Union[All, Sequence[str]]] = None,
    ) -> Union[dict[str, Any], Any]:
        """Create a run, wait until it finishes and return the final state.

        This method calls `POST /threads/{thread_id}/runs/wait` if a `thread_id`
        is speciffed in the `configurable` field of the config or
        `POST /runs/wait` otherwise.

        Args:
            input: Input to the graph.
            config: A `RunnableConfig` for graph invocation.
            interrupt_before: Interrupt the graph before these nodes.
            interrupt_after: Interrupt the graph after these nodes.

        Returns:
            The output of the graph.
        """
        async for chunk in self.astream(
            input,
            config=config,
            interrupt_before=interrupt_before,
            interrupt_after=interrupt_after,
            stream_mode="values",
        ):
            pass
        try:
            return chunk
        except UnboundLocalError:
            return None
