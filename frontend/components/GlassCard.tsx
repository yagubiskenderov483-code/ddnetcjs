'use client';
import { motion } from 'framer-motion';
import { ReactNode } from 'react';

export function GlassCard({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, ease: 'easeOut' }}
      className={`glass rounded-2xl p-6 md:p-8 shadow-glow ${className}`}
    >
      {children}
    </motion.div>
  );
}
