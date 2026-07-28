import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { RouterProvider } from 'react-router/dom';

import '@/index.css';
import { router } from '@/router';
import { OperatorCredentialProvider } from '@/shell/OperatorCredential';

const container = document.getElementById('root');
if (!container) {
  throw new Error('root container missing from index.html');
}

createRoot(container).render(
  <StrictMode>
    <OperatorCredentialProvider>
      <RouterProvider router={router} />
    </OperatorCredentialProvider>
  </StrictMode>,
);
