'use client';
import { motion } from 'framer-motion';
import type { PairingStatus } from '@/lib/types';

const STATE_LABEL: Record<string, string> = {
  awaiting_agent: 'ожидание агента',
  agent_connected: 'агент подключён',
  exporting: 'экспорт данных Telegram',
  uploading: 'загрузка архива на сервер',
  forwarding: 'отправка в Telegram-бота',
  done: 'готово',
  error: 'ошибка',
  expired: 'код истёк',
};

export function ExportProgress({ status }: { status: PairingStatus }) {
  const pct = Math.max(0, Math.min(100, status.progress));
  const isError = status.state === 'error' || status.state === 'expired';
  const isDone = status.state === 'done';

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between text-sm">
        <span className="text-white/70">
          {STATE_LABEL[status.state] || status.state}
          {status.stage ? ` · ${status.stage}` : ''}
        </span>
        <span className="font-mono text-white/90">{pct}%</span>
      </div>
      <div className="h-2 rounded-full bg-white/5 overflow-hidden">
        <motion.div
          className={`h-full ${isError ? 'bg-rose-400' : 'bg-gradient-to-r from-tg-blue to-cyan-400'}`}
          initial={{ width: 0 }}
          animate={{ width: `${pct}%` }}
          transition={{ duration: 0.4, ease: 'easeOut' }}
        />
      </div>
      {isError && status.error && (
        <div className="text-sm text-rose-300">{status.error}</div>
      )}
      {isDone && (
        <div className="text-sm text-emerald-300">
          Архив доставлен в бот. Временные файлы удалены.
        </div>
      )}
    </div>
  );
}
