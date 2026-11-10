export type PairingState =
  | 'awaiting_agent'
  | 'agent_connected'
  | 'exporting'
  | 'uploading'
  | 'forwarding'
  | 'done'
  | 'error'
  | 'expired';

export type PairingCreated = {
  pairing_code: string;
  polling_token: string;
  expires_in: number;
};

export type PairingStatus = {
  state: PairingState;
  progress: number;
  stage: string;
  delivered: boolean;
  error: string | null;
  agent_info: string | null;
};
