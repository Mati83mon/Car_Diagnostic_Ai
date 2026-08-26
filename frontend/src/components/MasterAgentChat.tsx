import { useEffect, useRef, useState, type FormEvent } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { CornerDownLeft, Loader2, Terminal, Wrench } from 'lucide-react'
import { cn } from '@/lib/cn'
import { timeOfDay } from '@/lib/format'
import { useTypewriter } from '@/hooks/useTypewriter'
import { CitationCard } from '@/components/CitationCard'
import type { AgentState, ChatEntry, ToolActivity } from '@/types/protocol'

const SUGGESTIONS = [
  'What faults are stored?',
  'Why is the turbo not making boost?',
  'Check every module for faults',
]

const AGENT_LABEL: Record<AgentState, string> = {
  idle: 'Ready',
  thinking: 'Reasoning',
  tool: 'Reading the vehicle',
  awaiting_approval: 'Awaiting your authorisation',
  error: 'Error',
}

interface MasterAgentChatProps {
  chat: ChatEntry[]
  tools: ToolActivity[]
  agentState: AgentState
  agentDetail: string
  connected: boolean
  onSend: (text: string) => void
  className?: string
}

/**
 * The diagnostic terminal.
 *
 * Tool calls stream in as they happen rather than appearing all at once with
 * the answer: a multi-step turn can take twenty seconds, and a silent panel
 * for twenty seconds reads as a hang.
 */
export function MasterAgentChat({
  chat,
  tools,
  agentState,
  agentDetail,
  connected,
  onSend,
  className,
}: MasterAgentChatProps) {
  const [draft, setDraft] = useState('')
  const scrollRef = useRef<HTMLDivElement>(null)
  const busy = agentState === 'thinking' || agentState === 'tool'

  useEffect(() => {
    const element = scrollRef.current
    if (element) element.scrollTop = element.scrollHeight
  }, [chat, tools.length, agentState])

  const submit = (event: FormEvent) => {
    event.preventDefault()
    const text = draft.trim()
    if (!text || !connected) return
    onSend(text)
    setDraft('')
  }

  const recentTools = busy ? tools.slice(-4) : []

  return (
    <section className={cn('glass flex min-h-0 flex-col', className)}>
      <header className="flex items-center justify-between gap-3 border-b border-white/[0.07] px-4 py-3">
        <div className="flex items-center gap-2">
          <Terminal className="h-3.5 w-3.5 text-neon" />
          <h2 className="panel-title">Majster-AI terminal</h2>
        </div>
        <div className="flex items-center gap-2">
          {busy && <Loader2 className="h-3 w-3 animate-spin text-neon" />}
          <span
            className={cn(
              'text-[10px] font-semibold uppercase tracking-[0.16em]',
              agentState === 'awaiting_approval'
                ? 'text-alert'
                : agentState === 'error'
                  ? 'text-alert'
                  : busy
                    ? 'text-neon'
                    : 'text-slate-500',
            )}
          >
            {AGENT_LABEL[agentState]}
          </span>
        </div>
      </header>

      <div ref={scrollRef} className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 py-4">
        {chat.length === 0 && <EmptyState onPick={onSend} disabled={!connected} />}

        {chat.map((entry) => (
          <ChatBubble key={entry.id} entry={entry} />
        ))}

        <AnimatePresence>
          {recentTools.map((tool) => (
            <motion.div
              key={tool.id}
              initial={{ opacity: 0, x: -8 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0 }}
              className="flex items-start gap-2 pl-1 font-mono text-[10.5px] text-slate-500"
            >
              <Wrench
                className={cn('mt-0.5 h-3 w-3 shrink-0', tool.ok ? 'text-neon/70' : 'text-alert/70')}
              />
              <span className="min-w-0">
                <span className={cn(tool.ok ? 'text-neon/80' : 'text-alert/80')}>{tool.tool}</span>
                {tool.summary && <span className="text-slate-600"> · {tool.summary}</span>}
              </span>
            </motion.div>
          ))}
        </AnimatePresence>

        {agentDetail && busy && (
          <p className="pl-1 font-mono text-[10.5px] italic text-slate-600">{agentDetail}</p>
        )}
      </div>

      <form onSubmit={submit} className="border-t border-white/[0.07] p-3">
        <div
          className={cn(
            'flex items-end gap-2 rounded-xl border bg-black/30 px-3 py-2 transition-colors',
            connected ? 'border-white/10 focus-within:border-neon/50' : 'border-alert/30',
          )}
        >
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) submit(event)
            }}
            rows={1}
            disabled={!connected}
            placeholder={
              connected ? 'Describe the symptom, or ask a question…' : 'Reconnecting…'
            }
            className={cn(
              'max-h-28 min-h-[24px] flex-1 resize-none bg-transparent text-sm text-slate-200',
              'placeholder:text-slate-600 focus:outline-none disabled:opacity-50',
            )}
          />
          <button
            type="submit"
            disabled={!connected || !draft.trim()}
            className={cn(
              'flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border transition-all',
              draft.trim() && connected
                ? 'border-neon/50 bg-neon/15 text-neon shadow-neon hover:bg-neon/25'
                : 'border-white/10 text-slate-600',
            )}
            aria-label="Send"
          >
            <CornerDownLeft className="h-3.5 w-3.5" />
          </button>
        </div>
      </form>
    </section>
  )
}

function EmptyState({
  onPick,
  disabled,
}: {
  onPick: (text: string) => void
  disabled: boolean
}) {
  return (
    <div className="py-6">
      <p className="mb-3 text-center text-xs leading-relaxed text-slate-500">
        Ask about a symptom and I'll read the vehicle, check the workshop manual
        and search the forums before answering.
      </p>
      <div className="flex flex-wrap justify-center gap-2">
        {SUGGESTIONS.map((suggestion) => (
          <button
            key={suggestion}
            type="button"
            disabled={disabled}
            onClick={() => onPick(suggestion)}
            className={cn(
              'rounded-full border border-white/10 bg-white/[0.03] px-3 py-1.5 text-[11px] text-slate-400',
              'transition-colors hover:border-neon/40 hover:text-neon disabled:opacity-40',
            )}
          >
            {suggestion}
          </button>
        ))}
      </div>
    </div>
  )
}

function ChatBubble({ entry }: { entry: ChatEntry }) {
  const isUser = entry.role === 'user'
  const body = useTypewriter(entry.text, entry.role === 'assistant' && entry.animate === true)

  if (entry.role === 'system') {
    return (
      <p className="px-1 font-mono text-[10.5px] text-slate-600">[system] {entry.text}</p>
    )
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.22 }}
      className={cn('flex flex-col', isUser ? 'items-end' : 'items-start')}
    >
      <div
        className={cn(
          'max-w-[92%] rounded-2xl px-3.5 py-2.5 text-sm leading-relaxed',
          isUser
            ? 'rounded-br-sm bg-neon text-[#04121f] shadow-neon'
            : 'rounded-bl-sm border border-neon/25 bg-black/35 text-slate-200 backdrop-blur-sm',
        )}
      >
        <p className="whitespace-pre-wrap">
          {body}
          {body.length < entry.text.length && (
            <span className="ml-0.5 inline-block h-3.5 w-[2px] translate-y-0.5 animate-breathe bg-neon" />
          )}
        </p>
      </div>

      {entry.citations.length > 0 && (
        <div className="mt-2 w-full max-w-[92%] space-y-1.5">
          {entry.citations.map((citation, index) => (
            <CitationCard key={`${entry.id}-${index}`} citation={citation} />
          ))}
        </div>
      )}

      <span className="mt-1 px-1 text-[9px] uppercase tracking-[0.14em] text-slate-600">
        {isUser ? 'You' : 'Majster-AI'} · {timeOfDay(entry.ts)}
        {entry.toolsUsed.length > 0 && ` · ${entry.toolsUsed.join(', ')}`}
      </span>
    </motion.div>
  )
}
