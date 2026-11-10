'use client';
import { AnimatePresence, motion } from 'framer-motion';
import { useEffect } from 'react';

export type ToastKind = 'success' | 'error' | 'info';

export function Toast({ open, kind, message, onClose }: {
  open: boolean; kind: ToastKind; message: string; onClose: () => void;
}) {
  useEffect(() => {
    if (!open) return;
    const t = setTimeout(onClose, 4500);
    return () => clearTimeout(t);
  }, [open, onClose]);

  const tone = {
    success: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200',
    error: 'border-rose-500/40 bg-rose-500/10 text-rose-200',
    info: 'border-tg-blue/40 bg-tg-blue/10 text-tg-blue',
  }[kind];

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0, y: 30 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: 20 }}
          className={`fixed bottom-6 left-1/2 -translate-x-1/2 px-4 py-3 rounded-xl border backdrop-blur-xl text-sm max-w-md ${tone}`}
        >
          {message}
        </motion.div>
      )}
    </AnimatePresence>
  );
}
