/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_SELTI_API_BASE?: string;
  readonly VITE_SELTI_TOKEN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

declare const __BUILD_ID__: string;
