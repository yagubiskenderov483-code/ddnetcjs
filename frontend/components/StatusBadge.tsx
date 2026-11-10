'use client';
import { motion } from 'framer-motion';

type Kind = 'idle' | 'connecting' | 'connected' | 'error';

export function StatusBadge({ kind, label }: { kind: Kind; label: string }) {
  const color = {
    idle: 'bg-white/10 text-white/60',
    connecting: 'bg-tg-blue/20 text-tg-blue',
    connected: 'bg-emerald-500/15 text-emerald-300',
    error: 'bg-rose-500/15 text-rose-300',
  }[kind];

  return (
    <motion.div
      layout
      className={`inline-flex items-center gap-2 px-3 py-1 rounded-full text-xs font-medium ${color}`}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${kind === 'connected' ? 'bg-emerald-400 pulse-ring' : kind === 'connecting' ? 'bg-tg-blue' : kind === 'error' ? 'bg-rose-400' : 'bg-white/40'}`} />
      {label}
    </motion.div>
  );
}
