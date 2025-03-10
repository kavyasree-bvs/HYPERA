#!/usr/bin/env python
# Foreground-Background Balance Agent - Specialized agent for optimizing class balance

import os
import sys
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Dict, List, Tuple, Any, Optional, Union
import logging
import time

# Add the parent directory to the path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from HYPERA1.segmentation.base_segmentation_agent import BaseSegmentationAgent
from HYPERA1.segmentation.segmentation_state_manager import SegmentationStateManager

class FGBalanceAgent(BaseSegmentationAgent):
    """
    Specialized agent for optimizing foreground-background balance in segmentation.
    
    This agent focuses on improving the foreground-background balance component of the reward function,
    which prevents over-segmentation (predicting too much foreground) or under-segmentation
    (predicting too little foreground) by encouraging a balance similar to the ground truth.
    
    Attributes:
        name (str): Name of the agent
        state_manager (SegmentationStateManager): Manager for shared state
        device (torch.device): Device to use for computation
        feature_extractor (nn.Module): Neural network for extracting features
        policy_network (nn.Module): Neural network for decision making
        optimizer (torch.optim.Optimizer): Optimizer for policy network
        learning_rate (float): Learning rate for optimizer
        gamma (float): Discount factor for future rewards
        update_frequency (int): Frequency of agent updates
        last_update_step (int): Last step when agent was updated
        action_history (List): History of actions taken by agent
        reward_history (List): History of rewards received by agent
        observation_history (List): History of observations
        verbose (bool): Whether to print verbose output
    """
    
    def __init__(
        self,
        state_manager: SegmentationStateManager,
        device: torch.device = None,
        state_dim: int = 10,
        action_dim: int = 1,
        action_space: Tuple[float, float] = (-1.0, 1.0),
        hidden_dim: int = 256,
        replay_buffer_size: int = 10000,
        batch_size: int = 64,
        gamma: float = 0.99,
        tau: float = 0.005,
        alpha: float = 0.2,
        lr: float = 3e-4,
        automatic_entropy_tuning: bool = True,
        update_frequency: int = 2,
        log_dir: str = "logs",
        verbose: bool = False
    ):
        """
        Initialize the foreground-background balance agent.
        
        Args:
            state_manager: Manager for shared state
            device: Device to use for computation
            state_dim: Dimension of state space
            action_dim: Dimension of action space
            action_space: Tuple of (min_action, max_action)
            hidden_dim: Dimension of hidden layers in networks
            replay_buffer_size: Size of replay buffer
            batch_size: Batch size for training
            gamma: Discount factor for future rewards
            tau: Target network update rate
            alpha: Temperature parameter for entropy
            lr: Learning rate
            automatic_entropy_tuning: Whether to automatically tune entropy
            update_frequency: Frequency of agent updates
            log_dir: Directory for saving logs and checkpoints
            verbose: Whether to print verbose output
        """
        # Store feature extraction parameters
        self.feature_channels = 32
        self.hidden_channels = 64
        
        super().__init__(
            name="FGBalanceAgent",
            state_manager=state_manager,
            device=device,
            state_dim=state_dim,
            action_dim=action_dim,
            action_space=action_space,
            hidden_dim=hidden_dim,
            replay_buffer_size=replay_buffer_size,
            batch_size=batch_size,
            gamma=gamma,
            tau=tau,
            alpha=alpha,
            lr=lr,
            automatic_entropy_tuning=automatic_entropy_tuning,
            update_frequency=update_frequency,
            log_dir=log_dir,
            verbose=verbose
        )
        
        # Initialize balance-specific components
        self._init_balance_components()
        
        if self.verbose:
            self.logger.info("Initialized FGBalanceAgent")
    
    def _init_balance_components(self):
        """
        Initialize balance-specific components.
        """
        # Feature extractor for balance features
        self.feature_extractor = nn.Sequential(
            nn.Conv2d(1, 16, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Conv2d(16, 32, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Conv2d(32, 64, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.AdaptiveAvgPool2d((8, 8))
        ).to(self.device)
        
        # Policy network for balance-specific actions
        self.policy_network = nn.Sequential(
            nn.Linear(64 * 8 * 8 + 2, 256),  # +2 for current fg ratio and target fg ratio
            nn.ReLU(),
            nn.Linear(256, 128),
            nn.ReLU(),
            nn.Linear(128, 64),
            nn.ReLU(),
            nn.Linear(64, 3)  # 3 actions: increase, decrease, or maintain threshold
        ).to(self.device)
        
        # Optimizer for policy network
        self.optimizer = torch.optim.Adam(
            list(self.feature_extractor.parameters()) + list(self.policy_network.parameters()),
            lr=self.lr
        )
        
        # Balance-specific parameters
        self.segmentation_threshold = 0.5  # Initial segmentation threshold
        self.min_threshold = 0.1
        self.max_threshold = 0.9
        self.threshold_step = 0.05
        
        # Target foreground ratio (will be updated based on ground truth)
        self.target_fg_ratio = 0.5
        
        # Moving average of foreground ratio
        self.avg_fg_ratio = 0.5
        self.avg_decay = 0.9  # Decay factor for moving average
    
    def observe(self) -> Dict[str, Any]:
        """
        Observe the current state.
        
        Returns:
            Dictionary of observations
        """
        # Get current state from state manager
        current_image = self.state_manager.get_current_image()
        current_mask = self.state_manager.get_current_mask()
        current_prediction = self.state_manager.get_current_prediction()
        
        if current_image is None or current_mask is None or current_prediction is None:
            # If any required state is missing, return empty observation
            return {}
        
        # Ensure tensors are on the correct device
        current_image = current_image.to(self.device)
        current_mask = current_mask.to(self.device)
        current_prediction = current_prediction.to(self.device)
        
        # Calculate foreground ratios
        gt_fg_ratio = torch.mean((current_mask > 0.5).float()).item()
        pred_fg_ratio = torch.mean((current_prediction > self.segmentation_threshold).float()).item()
        
        # Update target foreground ratio based on ground truth
        self.target_fg_ratio = gt_fg_ratio
        
        # Update moving average of foreground ratio
        self.avg_fg_ratio = self.avg_decay * self.avg_fg_ratio + (1 - self.avg_decay) * pred_fg_ratio
        
        # Calculate foreground-background balance metrics
        fg_balance_metrics = self._calculate_fg_balance_metrics(current_prediction, current_mask)
        
        # Get recent metrics from state manager
        recent_metrics = self.state_manager.get_recent_metrics()
        
        # Create observation dictionary
        observation = {
            "current_image": current_image,
            "current_mask": current_mask,
            "current_prediction": current_prediction,
            "gt_fg_ratio": gt_fg_ratio,
            "pred_fg_ratio": pred_fg_ratio,
            "avg_fg_ratio": self.avg_fg_ratio,
            "target_fg_ratio": self.target_fg_ratio,
            "segmentation_threshold": self.segmentation_threshold,
            "fg_balance_metrics": fg_balance_metrics,
            "recent_metrics": recent_metrics
        }
        
        # Store observation in history
        self.observation_history.append(observation)
        
        return observation
    
    def _calculate_fg_balance_metrics(self, prediction: torch.Tensor, ground_truth: torch.Tensor) -> Dict[str, float]:
        """
        Calculate foreground-background balance metrics.
        
        Args:
            prediction: Predicted segmentation mask
            ground_truth: Ground truth segmentation mask
            
        Returns:
            Dictionary of foreground-background balance metrics
        """
        # Ensure tensors are on the correct device
        prediction = prediction.to(self.device)
        ground_truth = ground_truth.to(self.device)
        
        # Calculate foreground ratios
        pred_fg_ratio = torch.mean((prediction > self.segmentation_threshold).float()).item()
        gt_fg_ratio = torch.mean((ground_truth > 0.5).float()).item()
        
        # Calculate absolute difference in foreground ratios
        fg_ratio_diff = abs(pred_fg_ratio - gt_fg_ratio)
        
        # Calculate class-wise metrics
        pred_binary = (prediction > self.segmentation_threshold).float()
        gt_binary = (ground_truth > 0.5).float()
        
        # True positives, false positives, false negatives
        tp = torch.sum(pred_binary * gt_binary).item()
        fp = torch.sum(pred_binary * (1 - gt_binary)).item()
        fn = torch.sum((1 - pred_binary) * gt_binary).item()
        tn = torch.sum((1 - pred_binary) * (1 - gt_binary)).item()
        
        # Calculate precision, recall, and F1 score
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1_score = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        
        # Calculate class imbalance metrics
        fg_pixels = torch.sum(gt_binary).item()
        bg_pixels = torch.sum(1 - gt_binary).item()
        total_pixels = fg_pixels + bg_pixels
        
        fg_weight = bg_pixels / total_pixels if total_pixels > 0 else 0.5
        bg_weight = fg_pixels / total_pixels if total_pixels > 0 else 0.5
        
        # Create metrics dictionary
        fg_balance_metrics = {
            "pred_fg_ratio": pred_fg_ratio,
            "gt_fg_ratio": gt_fg_ratio,
            "fg_ratio_diff": fg_ratio_diff,
            "precision": precision,
            "recall": recall,
            "f1_score": f1_score,
            "fg_weight": fg_weight,
            "bg_weight": bg_weight
        }
        
        return fg_balance_metrics
    
    def decide(self, observation: Dict[str, Any]) -> Dict[str, Any]:
        """
        Decide on an action based on the observation.
        
        Args:
            observation: Dictionary of observations
            
        Returns:
            Dictionary of actions
        """
        if not observation:
            # If observation is empty, return no action
            return {}
        
        # Extract features from current image
        current_image = observation["current_image"]
        
        # Ensure image has channel dimension
        if len(current_image.shape) == 3:
            current_image = current_image.unsqueeze(1)
        
        # Extract features
        features = self.feature_extractor(current_image)
        features = features.view(features.size(0), -1)
        
        # Add current foreground ratio and target foreground ratio to features
        pred_fg_ratio = observation["pred_fg_ratio"]
        target_fg_ratio = observation["target_fg_ratio"]
        
        # Concatenate features with foreground ratios
        extended_features = torch.cat([
            features,
            torch.tensor([[pred_fg_ratio, target_fg_ratio]], device=self.device)
        ], dim=1)
        
        # Get action logits from policy network
        action_logits = self.policy_network(extended_features)
        
        # Apply softmax to get action probabilities
        action_probs = F.softmax(action_logits, dim=1)
        
        # Sample action from probabilities
        if self.training:
            action = torch.multinomial(action_probs, 1).item()
        else:
            action = torch.argmax(action_probs, dim=1).item()
        
        # Map action to segmentation threshold adjustment
        if action == 0:
            # Increase threshold (decreases foreground)
            new_threshold = min(self.segmentation_threshold + self.threshold_step, self.max_threshold)
        elif action == 1:
            # Decrease threshold (increases foreground)
            new_threshold = max(self.segmentation_threshold - self.threshold_step, self.min_threshold)
        else:
            # Maintain current threshold
            new_threshold = self.segmentation_threshold
        
        # Create action dictionary
        action_dict = {
            "segmentation_threshold": new_threshold,
            "action_type": action,
            "action_probs": action_probs.detach().cpu().numpy(),
            "pred_fg_ratio": pred_fg_ratio,
            "target_fg_ratio": target_fg_ratio
        }
        
        # Store action in history
        self.action_history.append(action_dict)
        
        # Update segmentation threshold
        self.segmentation_threshold = new_threshold
        
        return action_dict
    
    def learn(self, reward: float) -> Dict[str, float]:
        """
        Learn from the reward.
        
        Args:
            reward: Reward value
            
        Returns:
            Dictionary of learning metrics
        """
        # Store reward in history
        self.reward_history.append(reward)
        
        # Check if it's time to update
        current_step = self.state_manager.get_current_step()
        if current_step - self.last_update_step < self.update_frequency:
            return {}
        
        # Update last update step
        self.last_update_step = current_step
        
        # Check if we have enough history for learning
        if len(self.action_history) < 2 or len(self.reward_history) < 2:
            return {}
        
        # Get the most recent observation, action, and reward
        observation = self.observation_history[-1]
        action = self.action_history[-1]
        
        # Extract features from current image
        current_image = observation["current_image"]
        
        # Ensure image has channel dimension
        if len(current_image.shape) == 3:
            current_image = current_image.unsqueeze(1)
        
        # Extract features
        features = self.feature_extractor(current_image)
        features = features.view(features.size(0), -1)
        
        # Add current foreground ratio and target foreground ratio to features
        pred_fg_ratio = observation["pred_fg_ratio"]
        target_fg_ratio = observation["target_fg_ratio"]
        
        # Concatenate features with foreground ratios
        extended_features = torch.cat([
            features,
            torch.tensor([[pred_fg_ratio, target_fg_ratio]], device=self.device)
        ], dim=1)
        
        # Get action logits from policy network
        action_logits = self.policy_network(extended_features)
        
        # Calculate policy loss using REINFORCE algorithm
        action_type = action["action_type"]
        policy_loss = -action_logits[0, action_type] * reward
        
        # Backpropagate and optimize
        self.optimizer.zero_grad()
        policy_loss.backward()
        self.optimizer.step()
        
        # Create learning metrics dictionary
        metrics = {
            "policy_loss": policy_loss.item(),
            "reward": reward,
            "segmentation_threshold": self.segmentation_threshold,
            "pred_fg_ratio": pred_fg_ratio,
            "target_fg_ratio": target_fg_ratio,
            "fg_ratio_diff": abs(pred_fg_ratio - target_fg_ratio)
        }
        
        return metrics
    
    def get_state(self) -> Dict[str, Any]:
        """
        Get the current state of the agent.
        
        Returns:
            Dictionary of agent state
        """
        return {
            "name": self.name,
            "segmentation_threshold": self.segmentation_threshold,
            "target_fg_ratio": self.target_fg_ratio,
            "avg_fg_ratio": self.avg_fg_ratio,
            "learning_rate": self.learning_rate,
            "gamma": self.gamma,
            "update_frequency": self.update_frequency,
            "last_update_step": self.last_update_step,
            "action_history_length": len(self.action_history),
            "reward_history_length": len(self.reward_history),
            "observation_history_length": len(self.observation_history),
            "training": self.training
        }
    
    def set_state(self, state: Dict[str, Any]) -> None:
        """
        Set the state of the agent.
        
        Args:
            state: Dictionary of agent state
        """
        if "segmentation_threshold" in state:
            self.segmentation_threshold = state["segmentation_threshold"]
        
        if "target_fg_ratio" in state:
            self.target_fg_ratio = state["target_fg_ratio"]
        
        if "avg_fg_ratio" in state:
            self.avg_fg_ratio = state["avg_fg_ratio"]
        
        if "learning_rate" in state:
            self.learning_rate = state["learning_rate"]
            # Update optimizer with new learning rate
            for param_group in self.optimizer.param_groups:
                param_group["lr"] = self.learning_rate
        
        if "gamma" in state:
            self.gamma = state["gamma"]
        
        if "update_frequency" in state:
            self.update_frequency = state["update_frequency"]
        
        if "training" in state:
            self.training = state["training"]
    
    def save(self, path: str) -> None:
        """
        Save the agent to a file.
        
        Args:
            path: Path to save the agent
        """
        # Create state dictionary
        state_dict = {
            "feature_extractor": self.feature_extractor.state_dict(),
            "policy_network": self.policy_network.state_dict(),
            "optimizer": self.optimizer.state_dict(),
            "segmentation_threshold": self.segmentation_threshold,
            "target_fg_ratio": self.target_fg_ratio,
            "avg_fg_ratio": self.avg_fg_ratio,
            "learning_rate": self.learning_rate,
            "gamma": self.gamma,
            "update_frequency": self.update_frequency,
            "last_update_step": self.last_update_step,
            "action_history": self.action_history,
            "reward_history": self.reward_history,
            "training": self.training
        }
        
        # Save state dictionary
        torch.save(state_dict, path)
        
        if self.verbose:
            self.logger.info(f"Saved FGBalanceAgent to {path}")
    
    def load(self, path: str) -> None:
        """
        Load the agent from a file.
        
        Args:
            path: Path to load the agent from
        """
        # Load state dictionary
        state_dict = torch.load(path, map_location=self.device)
        
        # Load model parameters
        self.feature_extractor.load_state_dict(state_dict["feature_extractor"])
        self.policy_network.load_state_dict(state_dict["policy_network"])
        self.optimizer.load_state_dict(state_dict["optimizer"])
        
        # Load agent parameters
        self.segmentation_threshold = state_dict["segmentation_threshold"]
        self.target_fg_ratio = state_dict["target_fg_ratio"]
        self.avg_fg_ratio = state_dict["avg_fg_ratio"]
        self.learning_rate = state_dict["learning_rate"]
        self.gamma = state_dict["gamma"]
        self.update_frequency = state_dict["update_frequency"]
        self.last_update_step = state_dict["last_update_step"]
        self.action_history = state_dict["action_history"]
        self.reward_history = state_dict["reward_history"]
        self.training = state_dict["training"]
        
        if self.verbose:
            self.logger.info(f"Loaded FGBalanceAgent from {path}")
    
    def _initialize_agent(self):
        """
        Initialize the agent's networks and components.
        """
        # Feature extractor for balance features
        self.feature_extractor = nn.Sequential(
            nn.Conv2d(1, 16, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Conv2d(16, 32, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Conv2d(32, 64, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.AdaptiveAvgPool2d((8, 8))
        ).to(self.device)
        
        # Policy network for balance-specific actions
        self.policy_network = nn.Sequential(
            nn.Linear(64 * 8 * 8 + 2, 256),  # +2 for current fg ratio and target fg ratio
            nn.ReLU(),
            nn.Linear(256, 128),
            nn.ReLU(),
            nn.Linear(128, 64),
            nn.ReLU(),
            nn.Linear(64, 3)  # 3 actions: increase, decrease, or maintain threshold
        ).to(self.device)
        
        # Optimizer for policy network
        self.optimizer = torch.optim.Adam(
            list(self.feature_extractor.parameters()) + list(self.policy_network.parameters()),
            lr=self.lr
        )
        
        # Balance-specific parameters
        self.segmentation_threshold = 0.5  # Initial segmentation threshold
        self.min_threshold = 0.1
        self.max_threshold = 0.9
        self.threshold_step = 0.05
        
        # Target foreground ratio (will be updated based on ground truth)
        self.target_fg_ratio = 0.5
        
        # Moving average of foreground ratio
        self.avg_fg_ratio = 0.5
        self.avg_decay = 0.9  # Decay factor for moving average
        
        if self.verbose:
            self.logger.info("Initialized FGBalanceAgent components")
    
    def _extract_features(self, observation):
        """
        Extract features from the observation.
        
        Args:
            observation: Dictionary containing observation data
            
        Returns:
            Extracted features
        """
        # Extract image and prediction from observation
        current_image = observation.get("current_image")
        current_prediction = observation.get("current_prediction")
        
        if current_image is None or current_prediction is None:
            # Return zero features if data is missing
            return torch.zeros((1, 64 * 8 * 8 + 2), device=self.device)
        
        # Ensure tensors are on the correct device
        current_image = current_image.to(self.device)
        current_prediction = current_prediction.to(self.device)
        
        # Get foreground ratios
        pred_fg_ratio = observation.get("pred_fg_ratio", 0.5)
        target_fg_ratio = observation.get("target_fg_ratio", 0.5)
        
        # Extract features using feature extractor
        # Use prediction as input to feature extractor
        if current_prediction.dim() == 3:
            # Add channel dimension if needed
            current_prediction = current_prediction.unsqueeze(1)
        elif current_prediction.dim() == 4:
            # Use first channel if multiple channels
            current_prediction = current_prediction[:, 0:1]
        
        # Extract features
        visual_features = self.feature_extractor(current_prediction)
        visual_features = visual_features.view(-1, 64 * 8 * 8)
        
        # Combine visual features with foreground ratio information
        ratio_features = torch.tensor([[pred_fg_ratio, target_fg_ratio]], device=self.device)
        combined_features = torch.cat([visual_features, ratio_features], dim=1)
        
        return combined_features
    
    def get_state_representation(self, observation):
        """
        Get state representation from observation.
        
        Args:
            observation: Dictionary containing observation data
            
        Returns:
            State representation
        """
        # Extract features from observation
        features = self._extract_features(observation)
        
        # Ensure the features have the correct shape for the SAC policy
        if isinstance(features, torch.Tensor):
            # If features is a tensor, reshape it to match the expected state_dim
            if features.dim() == 2:
                # If features is [batch_size, feature_dim]
                if features.shape[1] != self.state_dim:
                    # Resize to match state_dim
                    if features.shape[1] > self.state_dim:
                        # Truncate if too large
                        features = features[:, :self.state_dim]
                    else:
                        # Pad with zeros if too small
                        padding = torch.zeros(features.shape[0], self.state_dim - features.shape[1], device=features.device)
                        features = torch.cat([features, padding], dim=1)
            else:
                # If not 2D, reshape to [1, state_dim]
                features = features.view(1, -1)
                if features.shape[1] != self.state_dim:
                    # Resize to match state_dim
                    if features.shape[1] > self.state_dim:
                        # Truncate if too large
                        features = features[:, :self.state_dim]
                    else:
                        # Pad with zeros if too small
                        padding = torch.zeros(1, self.state_dim - features.shape[1], device=features.device)
                        features = torch.cat([features, padding], dim=1)
        elif isinstance(features, np.ndarray):
            # If features is a numpy array, reshape it to match the expected state_dim
            if len(features.shape) == 1:
                # If features is [feature_dim]
                features = features.reshape(1, -1)
            
            # Resize to match state_dim
            if features.shape[1] != self.state_dim:
                if features.shape[1] > self.state_dim:
                    # Truncate if too large
                    features = features[:, :self.state_dim]
                else:
                    # Pad with zeros if too small
                    padding = np.zeros((features.shape[0], self.state_dim - features.shape[1]), dtype=features.dtype)
                    features = np.concatenate([features, padding], axis=1)
            
            # Convert to tensor if needed
            features = torch.FloatTensor(features).to(self.device)
        
        # Return features as state representation
        return features
    
    def apply_action(self, action):
        """
        Apply the selected action to modify the segmentation.
        
        Args:
            action: Action to apply
            
        Returns:
            Modified segmentation
        """
        # Get current prediction from state manager
        current_prediction = self.state_manager.get_current_prediction()
        
        if current_prediction is None:
            # Return None if no prediction is available
            return None
        
        # Ensure tensor is on the correct device
        current_prediction = current_prediction.to(self.device)
        
        # Interpret action
        # Action is a tensor with shape [batch_size, action_dim]
        # For FGBalanceAgent, we interpret the action as a change to the segmentation threshold
        action_value = action.item() if isinstance(action, torch.Tensor) else action
        
        # Adjust threshold based on action
        if action_value < -0.33:  # Decrease threshold (increase foreground)
            self.segmentation_threshold = max(self.min_threshold, self.segmentation_threshold - self.threshold_step)
        elif action_value > 0.33:  # Increase threshold (decrease foreground)
            self.segmentation_threshold = min(self.max_threshold, self.segmentation_threshold + self.threshold_step)
        # Otherwise, maintain current threshold
        
        # Apply threshold to create binary segmentation
        binary_prediction = (current_prediction > self.segmentation_threshold).float()
        
        # Update prediction in state manager
        self.state_manager.set_current_prediction(binary_prediction)
        
        return binary_prediction
    
    def _save_agent_state(self, path):
        """
        Save agent state to file.
        
        Args:
            path: Path to save agent state
        """
        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(path), exist_ok=True)
        
        # Save model state
        state_dict = {
            'feature_extractor': self.feature_extractor.state_dict(),
            'policy_network': self.policy_network.state_dict(),
            'optimizer': self.optimizer.state_dict(),
            'segmentation_threshold': self.segmentation_threshold,
            'target_fg_ratio': self.target_fg_ratio,
            'avg_fg_ratio': self.avg_fg_ratio
        }
        
        # Save to file
        torch.save(state_dict, path)
        
        if self.verbose:
            self.logger.info(f"Saved FGBalanceAgent state to {path}")
    
    def _load_agent_state(self, path):
        """
        Load agent state from file.
        
        Args:
            path: Path to load agent state from
        """
        if not os.path.exists(path):
            if self.verbose:
                self.logger.warning(f"Agent state file {path} does not exist")
            return False
        
        try:
            # Load state dict
            state_dict = torch.load(path, map_location=self.device)
            
            # Load model state
            self.feature_extractor.load_state_dict(state_dict['feature_extractor'])
            self.policy_network.load_state_dict(state_dict['policy_network'])
            self.optimizer.load_state_dict(state_dict['optimizer'])
            self.segmentation_threshold = state_dict['segmentation_threshold']
            self.target_fg_ratio = state_dict['target_fg_ratio']
            self.avg_fg_ratio = state_dict['avg_fg_ratio']
            
            if self.verbose:
                self.logger.info(f"Loaded FGBalanceAgent state from {path}")
            
            return True
        except Exception as e:
            if self.verbose:
                self.logger.error(f"Error loading agent state: {e}")
            return False
    
    def _reset_agent(self):
        """
        Reset the agent to its initial state.
        """
        # Reset agent-specific parameters
        self.segmentation_threshold = 0.5
        self.target_fg_ratio = 0.5
        self.avg_fg_ratio = 0.5
        
        # Reset history
        self.action_history = []
        self.reward_history = []
        self.observation_history = []
        
        # Re-initialize networks
        self._initialize_agent()
        
        if self.verbose:
            self.logger.info("Reset FGBalanceAgent to initial state")
