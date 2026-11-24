// Anthropic Messages API <-> OpenAI Chat Completions translation.
//
// sub2api exposes /v1/messages so native Claude tooling can talk to it; the
// DeepAI lane only speaks OpenAI, so the gateway translates both ways,
// including the SSE event sequence Claude clients expect.

export function messagesToChat(body, { model }) {
  const messages = [];
  const system = body?.system;
  if (typeof system === "string" && system.trim()) {
    messages.push({ role: "system", content: system });
  } else if (Array.isArray(system)) {
    const text = system.filter((b) => b?.type === "text").map((b) => b.text).join("\n");
    if (text.trim()) messages.push({ role: "system", content: text });
  }

  for (const message of Array.isArray(body?.messages) ? body.messages : []) {
    const role = message?.role === "assistant" ? "assistant" : "user";
    const blocks = Array.isArray(message?.content) ? message.content : [{ type: "text", text: String(message?.content ?? "") }];

    const parts = [];
    const toolCalls = [];
    const toolResults = [];
    for (const block of blocks) {
      if (!block || typeof block !== "object") continue;
      if (block.type === "text") {
        parts.push({ type: "text", text: String(block.text ?? "") });
      } else if (block.type === "image" && block.source?.type === "base64") {
        parts.push({
          type: "image_url",
          image_url: { url: `data:${block.source.media_type ?? "image/png"};base64,${block.source.data ?? ""}` },
        });
      } else if (block.type === "tool_use") {
        toolCalls.push({
          id: block.id,
          type: "function",
          function: { name: block.name, arguments: JSON.stringify(block.input ?? {}) },
        });
      } else if (block.type === "tool_result") {
        const content = Array.isArray(block.content)
          ? block.content.filter((c) => c?.type === "text").map((c) => c.text).join("\n")
          : String(block.content ?? "");
        toolResults.push({ type: "tool_result", tool_call_id: block.tool_use_id, content });
      }
    }

    // Anthropic returns tool results as a user turn; OpenAI wants role:"tool".
    for (const result of toolResults) {
      messages.push({ role: "tool", tool_call_id: result.tool_call_id, content: result.content });
    }

    const hasBody = parts.length > 0;
    if (hasBody || toolCalls.length) {
      const content = parts.length === 1 && parts[0].type === "text" ? parts[0].text : parts;
      const entry = { role, content: hasBody ? content : null };
      if (toolCalls.length) entry.tool_calls = toolCalls;
      messages.push(entry);
    }
  }

  const chat = {
    model,
    messages,
    stream: body?.stream === true,
    ...(body?.max_tokens ? { max_tokens: body.max_tokens } : {}),
    ...(body?.temperature !== undefined ? { temperature: body.temperature } : {}),
    ...(body?.top_p !== undefined ? { top_p: body.top_p } : {}),
    ...(Array.isArray(body?.stop_sequences) && body.stop_sequences.length ? { stop: body.stop_sequences } : {}),
  };

  if (Array.isArray(body?.tools) && body.tools.length) {
    chat.tools = body.tools
      .filter((tool) => tool?.name)
      .map((tool) => ({
        type: "function",
        function: {
          name: tool.name,
          description: tool.description ?? "",
          parameters: tool.input_schema ?? { type: "object", properties: {} },
        },
      }));
    const choice = body?.tool_choice;
    if (choice?.type === "any") chat.tool_choice = "required";
    else if (choice?.type === "tool" && choice.name) chat.tool_choice = { type: "function", function: { name: choice.name } };
    else if (choice?.type === "auto") chat.tool_choice = "auto";
  }

  return chat;
}

const STOP_REASON = {
  stop: "end_turn",
  length: "max_tokens",
  tool_calls: "tool_use",
  function_call: "tool_use",
  content_filter: "end_turn",
};

export function chatToMessage(completion, { model, requestId }) {
  const choice = completion?.choices?.[0] ?? {};
  const message = choice.message ?? {};
  const content = [];

  if (typeof message.reasoning === "string" && message.reasoning) {
    content.push({ type: "thinking", thinking: message.reasoning });
  }
  if (typeof message.content === "string" && message.content) {
    content.push({ type: "text", text: message.content });
  } else if (Array.isArray(message.content)) {
    for (const part of message.content) {
      if (part?.type === "text" && part.text) content.push({ type: "text", text: part.text });
    }
  }
  for (const call of message.tool_calls ?? []) {
    let input = {};
    try {
      input = JSON.parse(call?.function?.arguments ?? "{}");
    } catch {
      input = {};
    }
    content.push({ type: "tool_use", id: call.id, name: call?.function?.name ?? "tool", input });
  }
  if (!content.length) content.push({ type: "text", text: "" });

  return {
    id: requestId ?? `msg_${completion?.id ?? "unknown"}`,
    type: "message",
    role: "assistant",
    model,
    content,
    stop_reason: STOP_REASON[choice.finish_reason] ?? "end_turn",
    stop_sequence: null,
    usage: {
      input_tokens: completion?.usage?.prompt_tokens ?? 0,
      output_tokens: completion?.usage?.completion_tokens ?? 0,
    },
  };
}

/**
 * Turn an OpenAI SSE stream into the Anthropic event sequence:
 *   message_start -> (content_block_start -> delta* -> content_block_stop)*
 *                 -> message_delta -> message_stop
 */
export async function* streamAsAnthropic(body, { model, requestId }) {
  const decoder = new TextDecoder();
  const encoder = new TextEncoder();
  const send = (event, data) => encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);

  yield send("message_start", {
    type: "message_start",
    message: {
      id: requestId ?? "msg_stream",
      type: "message",
      role: "assistant",
      model,
      content: [],
      stop_reason: null,
      stop_sequence: null,
      usage: { input_tokens: 0, output_tokens: 0 },
    },
  });

  let buffer = "";
  let textOpen = false;
  let thinkingOpen = false;
  let index = 0;
  const toolBlocks = new Map(); // openai tool_call index -> anthropic block index
  let finishReason = null;
  let usage = null;

  const openText = async function* () {
    if (textOpen) return;
    textOpen = true;
    yield send("content_block_start", { type: "content_block_start", index, content_block: { type: "text", text: "" } });
    index++;
  };

  for await (const chunk of body) {
    buffer += decoder.decode(chunk, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";

    for (const line of lines) {
      if (!line.startsWith("data:")) continue;
      const payload = line.slice(5).trim();
      if (!payload || payload === "[DONE]") continue;

      let parsed;
      try {
        parsed = JSON.parse(payload);
      } catch {
        continue;
      }
      if (parsed.usage) usage = parsed.usage;

      const delta = parsed.choices?.[0]?.delta ?? {};
      const finish = parsed.choices?.[0]?.finish_reason;
      if (finish) finishReason = finish;

      const reasoning = delta.reasoning ?? delta.reasoning_content;
      if (typeof reasoning === "string" && reasoning) {
        if (!thinkingOpen) {
          thinkingOpen = true;
          if (!textOpen) {
            yield send("content_block_start", { type: "content_block_start", index, content_block: { type: "thinking", thinking: "" } });
            thinkingOpen = true;
          } else {
            yield send("content_block_stop", { type: "content_block_stop", index: index - 1 });
            yield send("content_block_start", { type: "content_block_start", index, content_block: { type: "thinking", thinking: "" } });
          }
          thinkingOpen = true;
        }
        yield send("content_block_delta", {
          type: "content_block_delta",
          index,
          delta: { type: "thinking_delta", thinking: reasoning },
        });
      }

      if (typeof delta.content === "string" && delta.content) {
        if (thinkingOpen) {
          yield send("content_block_stop", { type: "content_block_stop", index });
          index++;
          thinkingOpen = false;
        }
        for await (const event of openText()) yield event;
        yield send("content_block_delta", {
          type: "content_block_delta",
          index: index - 1,
          delta: { type: "text_delta", text: delta.content },
        });
      }

      for (const call of delta.tool_calls ?? []) {
        const slot = call.index ?? 0;
        if (!toolBlocks.has(slot)) {
          if (thinkingOpen) {
            yield send("content_block_stop", { type: "content_block_stop", index });
            index++;
            thinkingOpen = false;
          }
          if (textOpen) {
            yield send("content_block_stop", { type: "content_block_stop", index: index - 1 });
            textOpen = false;
          } else if (!thinkingOpen) {
            yield send("content_block_stop", { type: "content_block_stop", index: Math.max(0, index - 1) });
          }
          toolBlocks.set(slot, index);
          yield send("content_block_start", {
            type: "content_block_start",
            index,
            content_block: { type: "tool_use", id: call.id ?? `tool_${slot}`, name: call.function?.name ?? "tool", input: {} },
          });
          index++;
        }
        if (call.function?.arguments) {
          yield send("content_block_delta", {
            type: "content_block_delta",
            index: toolBlocks.get(slot),
            delta: { type: "input_json_delta", partial_json: call.function.arguments },
          });
        }
      }
    }
  }

  // close whatever is open
  if (thinkingOpen) {
    yield send("content_block_stop", { type: "content_block_stop", index });
    index++;
  }
  if (textOpen) {
    yield send("content_block_stop", { type: "content_block_stop", index: index - 1 });
  }
  for (const blockIndex of toolBlocks.values()) {
    yield send("content_block_stop", { type: "content_block_stop", index: blockIndex });
  }

  yield send("message_delta", {
    type: "message_delta",
    delta: { stop_reason: STOP_REASON[finishReason] ?? "end_turn", stop_sequence: null },
    usage: { output_tokens: usage?.completion_tokens ?? 0 },
  });
  yield send("message_stop", { type: "message_stop" });
}