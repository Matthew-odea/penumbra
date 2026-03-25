import { Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import Feed from './pages/Feed'
import MarketView from './pages/MarketView'
import WalletView from './pages/WalletView'
import Wallets from './pages/Wallets'
import Metrics from './pages/Metrics'

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Feed />} />
        <Route path="/metrics" element={<Metrics />} />
        <Route path="/market/:marketId" element={<MarketView />} />
        <Route path="/wallet/:address" element={<WalletView />} />
        <Route path="/wallets" element={<Wallets />} />
      </Route>
    </Routes>
  )
}
