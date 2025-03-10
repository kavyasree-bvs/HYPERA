#!/usr/bin/env python
# Segmentation Agent Coordinator - Manages multiple segmentation agents

import os
import numpy as np
import torch
from typing import Dict, List, Tuple, Any, Optional, Union
import logging
import time

class SegmentationAgentCoordinator:
    """
    Coordinates multiple segmentation agents.
    
    This class manages the interactions between multiple segmentation agents,
    resolves conflicts, and combines their outputs to produce the final segmentation.
    
    Attributes:
        agents (list): List of segmentation agents
        state_manager: Reference to the shared state manager
        device (torch.device): Device to use for computation
        log_dir (str): Directory for saving logs and checkpoints
        verbose (bool): Whether to print verbose output
    """
    
    def __init__(
        self,
        agents: List,
        state_manager,
        device: torch.device = None,
        log_dir: str = "logs",
        verbose: bool = False,
        conflict_resolution: str = "weighted_average"
    ):
        """
        Initialize the segmentation agent coordinator.
        
        Args:
            agents: List of segmentation agents
            state_manager: Reference to the shared state manager
            device: Device to use for computation
            log_dir: Directory for saving logs and checkpoints
            verbose: Whether to print verbose output
            conflict_resolution: Method for resolving conflicts between agents
        """
        self.agents = agents
        self.state_manager = state_manager
        self.device = device if device is not None else torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.log_dir = log_dir
        self.verbose = verbose
        self.conflict_resolution = conflict_resolution
        
        # Create log directory if it doesn't exist
        os.makedirs(log_dir, exist_ok=True)
        
        # Set up logging
        self.logger = logging.getLogger("SegmentationAgentCoordinator")
        if verbose:
            self.logger.setLevel(logging.DEBUG)
        else:
            self.logger.setLevel(logging.INFO)
        
        # Initialize agent weights
        self.agent_weights = {agent.name: 1.0 for agent in agents}
        
        # Initialize performance tracking
        self.agent_performance = {agent.name: [] for agent in agents}
        
        if self.verbose:
            self.logger.info(f"Initialized Segmentation Agent Coordinator with {len(agents)} agents")
            for agent in agents:
                self.logger.info(f"  - {agent.name}")
    
    def process_observation(self, observation: torch.Tensor) -> Dict[str, Any]:
        """
        Process an observation with all agents.
        
        Args:
            observation: The current observation
            
        Returns:
            Dictionary of features extracted by each agent
        """
        features = {}
        
        # Set current image in state manager
        self.state_manager.set_current_image(observation)
        
        # Process observation with each agent
        for agent in self.agents:
            agent_features = agent.observe(observation)
            
            # Store features in state manager
            for key, value in agent_features.items():
                feature_key = f"{agent.name}_{key}"
                self.state_manager.set_feature(feature_key, value)
                features[feature_key] = value
        
        return features
    
    def make_decision(self, features: Dict[str, Any]) -> torch.Tensor:
        """
        Make a segmentation decision based on features from all agents.
        
        Args:
            features: Dictionary of features
            
        Returns:
            Segmentation decision tensor
        """
        agent_decisions = {}
        
        # Get decision from each agent
        for agent in self.agents:
            # Extract features relevant to this agent
            agent_features = {
                k.replace(f"{agent.name}_", ""): v
                for k, v in features.items()
                if k.startswith(f"{agent.name}_")
            }
            
            # Get decision from agent
            decision = agent.decide(agent_features)
            agent_decisions[agent.name] = decision
        
        # Resolve conflicts and combine decisions
        combined_decision = self._resolve_conflicts(agent_decisions)
        
        # Set current prediction in state manager
        self.state_manager.set_current_prediction(combined_decision)
        
        return combined_decision
    
    def _resolve_conflicts(self, agent_decisions: Dict[str, torch.Tensor]) -> torch.Tensor:
        """
        Resolve conflicts between agent decisions.
        
        Args:
            agent_decisions: Dictionary of decisions from each agent
            
        Returns:
            Combined decision tensor
        """
        if self.conflict_resolution == "weighted_average":
            # Weighted average of all decisions
            combined = None
            total_weight = 0
            
            for agent_name, decision in agent_decisions.items():
                weight = self.agent_weights.get(agent_name, 1.0)
                if combined is None:
                    combined = weight * decision
                else:
                    combined += weight * decision
                total_weight += weight
            
            if total_weight > 0:
                combined /= total_weight
            
            return combined
        
        elif self.conflict_resolution == "priority":
            # Use decision from highest priority agent
            priority_order = sorted(
                self.agent_weights.items(),
                key=lambda x: x[1],
                reverse=True
            )
            
            for agent_name, _ in priority_order:
                if agent_name in agent_decisions:
                    return agent_decisions[agent_name]
            
            # Fallback to first agent if none found
            return next(iter(agent_decisions.values()))
        
        elif self.conflict_resolution == "voting":
            # Binary voting for each pixel
            decisions = list(agent_decisions.values())
            if not decisions:
                return torch.zeros_like(self.state_manager.current_image)
            
            # Stack decisions and compute mean
            stacked = torch.stack(decisions)
            mean_decision = torch.mean(stacked, dim=0)
            
            # Apply threshold
            return (mean_decision > 0.5).float()
        
        else:
            # Default to first agent
            return next(iter(agent_decisions.values()))
    
    def update_agents(self, reward_dict: Dict[str, float], done: bool = False) -> Dict[str, Dict[str, float]]:
        """
        Update all agents with rewards.
        
        Args:
            reward_dict: Dictionary of rewards
            done: Whether the episode is done
            
        Returns:
            Dictionary of update metrics for each agent
        """
        update_metrics = {}
        
        # Update each agent
        for agent in self.agents:
            # Get agent-specific reward
            agent_reward = reward_dict.get(agent.name, reward_dict.get("total", 0.0))
            
            # Update agent
            metrics = agent.update(agent_reward, done)
            update_metrics[agent.name] = metrics
            
            # Track agent performance
            self.agent_performance[agent.name].append(agent_reward)
        
        # Update agent weights based on recent performance
        self._update_agent_weights()
        
        return update_metrics
    
    def _update_agent_weights(self, window: int = 10):
        """
        Update agent weights based on recent performance.
        
        Args:
            window: Number of recent rewards to consider
        """
        # Only update weights if we have enough history
        for agent_name, rewards in self.agent_performance.items():
            if len(rewards) >= window:
                # Calculate average recent reward
                recent_avg = np.mean(rewards[-window:])
                
                # Update weight (ensure it's positive)
                self.agent_weights[agent_name] = max(0.1, recent_avg)
        
        # Normalize weights
        total_weight = sum(self.agent_weights.values())
        if total_weight > 0:
            for agent_name in self.agent_weights:
                self.agent_weights[agent_name] /= total_weight
                self.agent_weights[agent_name] *= len(self.agent_weights)  # Scale to average of 1.0
        
        if self.verbose:
            self.logger.debug(f"Updated agent weights: {self.agent_weights}")
    
    def act(self, observation: torch.Tensor) -> torch.Tensor:
        """
        Process an observation and return a segmentation decision.
        
        Args:
            observation: The current observation
            
        Returns:
            Segmentation decision tensor
        """
        features = self.process_observation(observation)
        decision = self.make_decision(features)
        return decision
    
    def save_agents(self, path: Optional[str] = None) -> Dict[str, str]:
        """
        Save all agents.
        
        Args:
            path: Base path to save agents. If None, use default path.
            
        Returns:
            Dictionary of agent names and their save paths
        """
        if path is None:
            path = self.log_dir
        
        save_paths = {}
        
        for agent in self.agents:
            agent_path = os.path.join(path, f"{agent.name}_agent.pt")
            save_paths[agent.name] = agent.save(agent_path)
        
        # Save coordinator state
        coordinator_path = os.path.join(path, "agent_coordinator.pt")
        torch.save({
            "agent_weights": self.agent_weights,
            "agent_performance": self.agent_performance,
            "conflict_resolution": self.conflict_resolution
        }, coordinator_path)
        
        if self.verbose:
            self.logger.info(f"Saved all agents to {path}")
        
        return save_paths
    
    def load_agents(self, path: str) -> bool:
        """
        Load all agents.
        
        Args:
            path: Base path to load agents from
            
        Returns:
            Whether the load was successful
        """
        success = True
        
        for agent in self.agents:
            agent_path = os.path.join(path, f"{agent.name}_agent.pt")
            if not agent.load(agent_path):
                success = False
        
        # Load coordinator state
        coordinator_path = os.path.join(path, "agent_coordinator.pt")
        if os.path.exists(coordinator_path):
            try:
                checkpoint = torch.load(coordinator_path, map_location=self.device)
                self.agent_weights = checkpoint["agent_weights"]
                self.agent_performance = checkpoint["agent_performance"]
                self.conflict_resolution = checkpoint["conflict_resolution"]
            except Exception as e:
                self.logger.error(f"Failed to load coordinator state: {e}")
                success = False
        
        if self.verbose:
            if success:
                self.logger.info(f"Loaded all agents from {path}")
            else:
                self.logger.warning(f"Failed to load some agents from {path}")
        
        return success
    
    def reset(self):
        """Reset all agents."""
        for agent in self.agents:
            agent.reset()
        
        if self.verbose:
            self.logger.info("Reset all agents")

    def refine_segmentation(self, initial_preds: torch.Tensor) -> torch.Tensor:
        """
        Refine segmentation predictions using the agents.
        
        Args:
            initial_preds: Initial segmentation predictions from the base model
            
        Returns:
            Refined segmentation predictions
        """
        # Update the state manager with the initial predictions
        self.state_manager.update_segmentation(initial_preds)
        
        # Create observation from initial predictions
        # This typically includes the initial segmentation and any relevant features
        observation = {
            'current_segmentation': initial_preds,
            'ground_truth': self.state_manager.get_current_ground_truth()
        }
        
        # Process the observation with all agents to get features
        features = {}
        for agent in self.agents:
            # Get state representation from the agent
            state = agent.get_state_representation(observation)
            
            # Ensure state is a tensor with the correct shape for the SAC policy
            if isinstance(state, np.ndarray):
                if len(state.shape) == 1:
                    # If state is [state_dim], reshape to [1, state_dim]
                    state = state.reshape(1, -1)
                # Convert to tensor
                state = torch.FloatTensor(state).to(self.device)
            elif isinstance(state, torch.Tensor):
                if state.dim() == 1:
                    # If state is [state_dim], reshape to [1, state_dim]
                    state = state.unsqueeze(0)
            
            # Use the agent's policy to select an action
            action = agent.sac.select_action(state)
            
            # Apply the action to get refined segmentation
            # Handle different agent implementations (some require features, some don't)
            try:
                # First try with observation parameter
                agent_refined = agent.apply_action(action, observation)
            except TypeError:
                # If that fails, try without observation parameter
                agent_refined = agent.apply_action(action)
            
            # Store the agent's refinement
            features[agent.name] = agent_refined
        
        # Combine the refinements from all agents
        if not features:
            # If no refinements, return the initial predictions
            return initial_preds
        
        # Use weighted averaging to combine refinements
        combined = None
        total_weight = 0
        
        for agent_name, refinement in features.items():
            # Skip None refinements
            if refinement is None:
                print(f"Warning: Agent {agent_name} returned None refinement. Skipping.")
                continue
                
            weight = self.agent_weights.get(agent_name, 1.0)
            if combined is None:
                combined = weight * refinement
            else:
                combined += weight * refinement
            total_weight += weight
        
        # If all refinements were None, return the initial predictions
        if combined is None or total_weight == 0:
            print("Warning: All agents returned None refinements. Using initial predictions.")
            return initial_preds
            
        if total_weight > 0:
            combined /= total_weight
        
        # Apply thresholding to get binary segmentation
        refined_preds = (combined > 0.5).float()
        
        # Update the state manager with the refined segmentation
        self.state_manager.update_segmentation(refined_preds)
        
        return refined_preds

    def save(self, path):
        """
        Save the coordinator and its agents.
        
        Args:
            path: Path to save the coordinator
        """
        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(path), exist_ok=True)
        
        # Save coordinator state
        state_dict = {
            'conflict_resolution': self.conflict_resolution,
            'agent_names': [agent.name for agent in self.agents]
        }
        
        # Save the coordinator state
        torch.save(state_dict, path)
        
        # Save each agent in the same directory
        for agent in self.agents:
            agent_path = os.path.join(os.path.dirname(path), f"{agent.name}_agent.pt")
            agent.save(agent_path)
            
        if self.verbose:
            logging.info(f"Saved coordinator to {path}")
            
    def load(self, path):
        """
        Load the coordinator and its agents.
        
        Args:
            path: Path to load the coordinator from
        """
        # Load coordinator state
        state_dict = torch.load(path)
        
        # Set coordinator attributes
        self.conflict_resolution = state_dict['conflict_resolution']
        
        # Load each agent
        for agent_name in state_dict['agent_names']:
            agent_path = os.path.join(os.path.dirname(path), f"{agent_name}_agent.pt")
            # Find the agent with this name
            for agent in self.agents:
                if agent.name == agent_name:
                    agent.load(agent_path)
                    break
                    
        if self.verbose:
            logging.info(f"Loaded coordinator from {path}")
