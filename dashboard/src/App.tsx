import { Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import Feed from './pages/Feed'
import MarketView from './pages/MarketView'
import WalletView from './pages/WalletView'

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Feed />} />
        <Route path="/market/:marketId" element={<MarketView />} />
        <Route path="/wallet/:address" element={<WalletView />} />
      </Route>
    </Routes>
  )
}
