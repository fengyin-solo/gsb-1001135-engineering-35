import { createRouter, createWebHistory } from 'vue-router'

import Dashboard from '@/views/Dashboard.vue'
const RoadSection = () => import('@/views/road_section/index.vue')
const Patrol = () => import('@/views/patrol/index.vue')
const Pavement = () => import('@/views/pavement/index.vue')
const Bridge = () => import('@/views/bridge/index.vue')
const BridgeInfo = () => import('@/views/bridge_info/index.vue')
const Tunnel = () => import('@/views/tunnel/index.vue')
const TrafficFacility = () => import('@/views/traffic_facility/index.vue')
const Drainage = () => import('@/views/drainage/index.vue')
const Green = () => import('@/views/green/index.vue')
const Lighting = () => import('@/views/lighting/index.vue')
const Winter = () => import('@/views/winter/index.vue')
const Flood = () => import('@/views/flood/index.vue')
const Slope = () => import('@/views/slope/index.vue')
const Expansion = () => import('@/views/expansion/index.vue')
const Bearing = () => import('@/views/bearing/index.vue')
const Project = () => import('@/views/project/index.vue')
const Vehicle = () => import('@/views/vehicle/index.vue')
const Material = () => import('@/views/material/index.vue')
const Release = () => import('@/views/release/index.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: Dashboard },
    { path: '/road_section', name: 'road_section', component: RoadSection },
    { path: '/patrol', name: 'patrol', component: Patrol },
    { path: '/pavement', name: 'pavement', component: Pavement },
    { path: '/bridge', name: 'bridge', component: Bridge },
    { path: '/bridge_info', name: 'bridge_info', component: BridgeInfo },
    { path: '/tunnel', name: 'tunnel', component: Tunnel },
    { path: '/traffic_facility', name: 'traffic_facility', component: TrafficFacility },
    { path: '/drainage', name: 'drainage', component: Drainage },
    { path: '/green', name: 'green', component: Green },
    { path: '/lighting', name: 'lighting', component: Lighting },
    { path: '/winter', name: 'winter', component: Winter },
    { path: '/flood', name: 'flood', component: Flood },
    { path: '/slope', name: 'slope', component: Slope },
    { path: '/expansion', name: 'expansion', component: Expansion },
    { path: '/bearing', name: 'bearing', component: Bearing },
    { path: '/project', name: 'project', component: Project },
    { path: '/vehicle', name: 'vehicle', component: Vehicle },
    { path: '/material', name: 'material', component: Material },
    { path: '/release', name: 'release', component: Release },
  ],
})

export default router
