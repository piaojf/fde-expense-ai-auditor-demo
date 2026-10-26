import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import Dashboard from './pages/Dashboard';
import ClaimsList from './pages/ClaimsList';
import ClaimDetail from './pages/ClaimDetail';
import HumanReview from './pages/HumanReview';
import AuditLog from './pages/AuditLog';
import NewClaim from './pages/NewClaim';
import Policies from './pages/Policies';
import EnterpriseRoutes from './pages/EnterprisePortal';
import { ENTERPRISE_MODE } from './lib/api/enterprise';
import { AuthProvider } from './lib/session/AuthContext';
import './styles/enterprise.css';

export default function App() {
  if (ENTERPRISE_MODE) return <BrowserRouter><AuthProvider><EnterpriseRoutes /></AuthProvider></BrowserRouter>;
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/claims" element={<ClaimsList />} />
        <Route path="/policies" element={<Policies />} />
        <Route path="/claims/new" element={<NewClaim />} />
        <Route path="/claims/:id" element={<ClaimDetail />} />
        <Route path="/claims/:id/review" element={<HumanReview />} />
        <Route path="/claims/:id/audit" element={<AuditLog />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
