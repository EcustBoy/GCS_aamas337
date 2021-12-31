import os
from network.base_net import RNN
from network.vdn_net import VDNNet
from network.action_graph_net2 import *
import torch
from torch.optim import lr_scheduler
import torch.nn.functional as F
from common.utils import *
from torch.autograd import Variable
from common.utils import _h_A, update_optimizer
import numpy as np
class VDN_GRAPH_2:
    def __init__(self, args):
        self.n_actions = args.n_actions
        self.n_agents = args.n_agents
        self.state_shape = args.state_shape
        self.obs_shape = args.obs_shape
        input_shape = self.obs_shape
        # 根据参数决定RNN的输入维度
        if args.last_action:
            input_shape += self.n_actions
        if args.reuse_network:
            input_shape += self.n_agents
        if args.father_action:
            input_shape += self.n_agents * self.n_actions

        # graph_input_shape = input_shape
        # if args.father_action:
        #     graph_input_shape += self.n_agents * self.n_actions

        # input_shape += input_shape

        # 神经网络
        self.eval_rnn = RNN(input_shape, args)  # 每个agent选动作的网络
        self.target_rnn = RNN(input_shape, args)
        self.eval_vdn_net = VDNNet()  # 把agentsQ值加起来的网络
        self.target_vdn_net = VDNNet()

        self.actor = Actor_graph(args)
        self.critic = Critic_graph(args)

        self.target_actor = Actor_graph(args)
        self.target_critic = Critic_graph(args)

        self.args = args
        if self.args.cuda:
            self.eval_rnn.cuda()
            self.target_rnn.cuda()
            self.eval_vdn_net.cuda()
            self.target_vdn_net.cuda()
            self.actor.cuda()
            self.critic.cuda()
            self.target_actor.cuda()
            self.target_critic.cuda()

        self.model_dir = args.model_dir + '/' + args.alg + '/' + args.map


        # 让target_net和eval_net的网络参数相同
        self.target_rnn.load_state_dict(self.eval_rnn.state_dict())
        self.target_vdn_net.load_state_dict(self.eval_vdn_net.state_dict())
        self.target_actor.load_state_dict(self.actor.state_dict())
        self.target_critic.load_state_dict(self.critic.state_dict())

        self.eval_parameters = list(self.eval_vdn_net.parameters()) + list(self.eval_rnn.parameters()) + list(self.actor.parameters()) + list(self.critic.parameters())
        # self.graph_actor_parameters = list(self.actor.parameters())
        # self.graph_critic_parameters = list(self.critic.parameters())
        # self.dag_gnn_parameters = list(self.encoder.parameters()) + list(self.decoder.parameters())
        if args.optimizer == "RMS":
            self.optimizer = torch.optim.RMSprop(self.eval_parameters, lr=args.lr)
            self.eval_scheduler = lr_scheduler.StepLR(self.optimizer, step_size=args.lr_decay, gamma=args.gamma_gnn)
            # self.graph_actor_optimizer = torch.optim.RMSprop(self.graph_actor_parameters, lr=args.lr)
            # self.graph_critic_optimizer = torch.optim.RMSprop(self.graph_critic_parameters, lr=args.lr)
        # self.dag_gnn_optimizer = optim.Adam(self.dag_gnn_parameters, lr=args.lr)
        # self.dag_gnn_scheduler = lr_scheduler.StepLR(self.dag_gnn_optimizer, step_size=args.lr_decay, gamma=args.gamma_gnn)

        # 执行过程中，要为每个agent都维护一个eval_hidden
        # 学习过程中，要为每个episode的每个agent都维护一个eval_hidden、target_hidden
        self.eval_hidden = None
        self.target_hidden = None
        print('Init alg VDN_GRAPH_2')

    def learn(self, batch, max_episode_len, train_step, epsilon_=None):  # train_step表示是第几次学习，用来控制更新target_net网络的参数
        '''
        在learn的时候，抽取到的数据是四维的，四个维度分别为 1——第几个episode 2——episode中第几个transition
        3——第几个agent的数据 4——具体obs维度。因为在选动作时不仅需要输入当前的inputs，还要给神经网络输入hidden_state，
        hidden_state和之前的经验相关，因此就不能随机抽取经验进行学习。所以这里一次抽取多个episode，然后一次给神经网络
        传入每个episode的同一个位置的transition
        '''
        episode_num = batch['o'].shape[0]
        self.init_hidden(episode_num)
        for key in batch.keys():  # 把batch里的数据转化成tensor
            if key == 'u':
                batch[key] = torch.tensor(batch[key], dtype=torch.long)
            else:
                batch[key] = torch.tensor(batch[key], dtype=torch.float32)
        # TODO pymarl中取得经验没有取最后一条，找出原因
        u, r, avail_u, avail_u_next, terminated = batch['u'], batch['r'],  batch['avail_u'], \
                                                  batch['avail_u_next'], batch['terminated']
        mask = 1 - batch["padded"].float()  # 用来把那些填充的经验的TD-error置0，从而不让它们影响到学习
        if self.args.cuda:
            u = u.cuda()
            r = r.cuda()
            mask = mask.cuda()
            terminated = terminated.cuda()
        # 得到每个agent对应的Q值，维度为(episode个数, max_episode_len， n_agents，n_actions)
        # q_evals, q_targets = self.get_q_values(batch, max_episode_len)
        q_evals, q_targets, action_prob = self.get_q_values_new(batch, max_episode_len, epsilon_)  ### rjq_new0730
        q_evals_ = q_evals.clone()

        # 取每个agent动作对应的Q值，并且把最后不需要的一维去掉，因为最后一维只有一个值了
        q_evals = torch.gather(q_evals, dim=3, index=u).squeeze(3)

        # 得到target_q
        q_targets[avail_u_next == 0.0] = - 9999999
        q_targets = q_targets.max(dim=3)[0]

        q_total_eval = self.eval_vdn_net(q_evals)  # torch.Size([1, 25, 1])
        q_total_target = self.target_vdn_net(q_targets)

        r_ = 0.5 * torch.sum(r, dim=-1).reshape(q_total_eval.shape[0], -1, 1)
        targets = r_ + self.args.gamma * q_total_target * (1 - terminated)

        # targets = r[:,:,:,0] + self.args.gamma * q_total_target * (1 - terminated)

        td_error = targets.detach() - q_total_eval
        masked_td_error = mask * td_error  # 抹掉填充的经验的td_error

        # loss = masked_td_error.pow(2).mean()
        # 不能直接用mean，因为还有许多经验是没用的，所以要求和再比真实的经验数，才是真正的均值
        loss_policy = (masked_td_error ** 2).sum() / mask.sum()

        # # print('Loss is ', loss)
        # self.optimizer.zero_grad()
        # loss_policy.backward()
        # torch.nn.utils.clip_grad_norm_(self.eval_parameters, self.args.grad_norm_clip)
        # self.optimizer.step()

        #########################################################################
        # graph_loss = self.train_dag_gnn_all(batch, self.args.lambda_A, self.args.c_A)
        ########################
        # q_evals_, q_targets = self.get_q_values(batch, max_episode_len)  ### rjq_new0730


        loss_graph_actor, loss_graph_critic, loss_hA = self.train_dag_rl(batch, action_prob, q_evals_)

        self.optimizer.zero_grad()
        (loss_policy + loss_graph_actor + loss_graph_critic).backward()
        torch.nn.utils.clip_grad_norm_(self.eval_parameters, self.args.grad_norm_clip)
        self.optimizer.step()
        self.eval_scheduler.step()

        ################## loss_graph_critic

        # self.graph_critic_optimizer.zero_grad()
        # loss_graph_critic.backward()
        # torch.nn.utils.clip_grad_norm_(self.graph_critic_parameters, self.args.grad_norm_clip)
        # self.graph_critic_optimizer.step()

        if train_step > 0 and train_step % self.args.target_update_cycle == 0:
            self.target_rnn.load_state_dict(self.eval_rnn.state_dict())
            self.target_vdn_net.load_state_dict(self.eval_vdn_net.state_dict())

            self.target_actor.load_state_dict(self.actor.state_dict())
            self.target_critic.load_state_dict(self.critic.state_dict())

        return loss_policy, loss_graph_actor, loss_graph_critic, loss_hA

    def _get_inputs(self, batch, transition_idx):
        # 取出所有episode上该transition_idx的经验，u_onehot要取出所有，因为要用到上一条
        obs, obs_next, u_onehot, father_actions, father_actions_next = batch['o'][:, transition_idx], \
                                  batch['o_next'][:, transition_idx], batch['u_onehot'][:], \
                                  batch['father_actions'][:], batch['father_actions_next'][:]
        episode_num = obs.shape[0]
        inputs, inputs_next = [], []
        inputs.append(obs)
        inputs_next.append(obs_next)

        # 给obs添加上一个动作、agent编号
        if self.args.last_action:
            if transition_idx == 0:  # 如果是第一条经验，就让前一个动作为0向量
                inputs.append(torch.zeros_like(u_onehot[:, transition_idx]))
            else:
                inputs.append(u_onehot[:, transition_idx - 1])
            inputs_next.append(u_onehot[:, transition_idx])
        if self.args.reuse_network:
            # 因为当前的obs三维的数据，每一维分别代表(episode，agent，obs维度)，直接在dim_1上添加对应的向量
            # 即可，比如给agent_0后面加(1, 0, 0, 0, 0)，表示5个agent中的0号。而agent_0的数据正好在第0行，那么需要加的
            # agent编号恰好就是一个单位矩阵，即对角线为1，其余为0
            inputs.append(torch.eye(self.args.n_agents).unsqueeze(0).expand(episode_num, -1, -1))
            inputs_next.append(torch.eye(self.args.n_agents).unsqueeze(0).expand(episode_num, -1, -1))
        # 要把obs中的三个拼起来，并且要把episode_num个episode、self.args.n_agents个agent的数据拼成episode_num*n_agents条数据
        # 因为这里所有agent共享一个神经网络，每条数据中带上了自己的编号，所以还是自己的数据

        if self.args.father_action:
            inputs.append(father_actions[:,transition_idx,:])
            inputs_next.append(father_actions_next[:,transition_idx,:,])

        inputs = torch.cat([x.reshape(episode_num * self.args.n_agents, -1) for x in inputs], dim=1)
        inputs_next = torch.cat([x.reshape(episode_num * self.args.n_agents, -1) for x in inputs_next], dim=1)
        return inputs, inputs_next ## torch.Size([3, 31])


    def get_q_values_new(self, batch, max_episode_len, epsilon_):
        episode_num = batch['o'].shape[0]
        avail_actions = batch['avail_u']
        q_evals, q_targets, action_prob = [], [], []
        for transition_idx in range(max_episode_len):
            inputs, inputs_next = self._get_inputs(batch, transition_idx)  # 给obs加last_action、agent_id
            if self.args.cuda:
                inputs = inputs.cuda()
                inputs_next = inputs_next.cuda()
                self.eval_hidden = self.eval_hidden.cuda()
                self.target_hidden = self.target_hidden.cuda()
            q_eval, self.eval_hidden = self.eval_rnn(inputs, self.eval_hidden)  # 得到的q_eval维度为(episode_num*n_agents, n_actions)
            q_target, self.target_hidden = self.target_rnn(inputs_next, self.target_hidden)

            ### ----------------------------rjq add pro
            q_eval_l = q_eval.clone()  ### rjq add pro
            # q_eval_l = q_eval_l.view(episode_num, 1, -1)
            q_eval_l = q_eval_l.view(episode_num, self.n_agents, -1)
            prob = torch.nn.functional.softmax(q_eval_l, dim=-1) # torch.Size([5, 5])
            # prob = prob.view(episode_num, -1, q_eval.shape[-1]) # torch.Size([5, 5])
            action_prob.append(prob)

            # 把q_eval维度重新变回(episode_num, n_agents, n_actions)
            q_eval = q_eval.view(episode_num, self.n_agents, -1)
            q_target = q_target.view(episode_num, self.n_agents, -1)
            q_evals.append(q_eval)
            q_targets.append(q_target)
        # 得的q_eval和q_target是一个列表，列表里装着max_episode_len个数组，数组的的维度是(episode个数, n_agents，n_actions)
        # 把该列表转化成(episode个数, max_episode_len， n_agents，n_actions)的数组
        q_evals = torch.stack(q_evals, dim=1)
        q_targets = torch.stack(q_targets, dim=1)

        ### ----------------------------rjq add pro
        action_prob = torch.stack(action_prob, dim=1).cpu()  # torch.Size([1, 25, 5, 5])
        action_num = avail_actions.sum(dim=-1, keepdim=True).float().repeat(1, 1, 1, avail_actions.shape[-1])  # 可以选择的动作的个数 # torch.Size([1, 25, 5, 5])
        action_prob = ((1 - epsilon_) * action_prob + torch.ones_like(action_prob) * epsilon_ / action_num)
        action_prob[avail_actions == 0] = 0.0  # 不能执行的动作概率为0

        # 因为上面把不能执行的动作概率置为0，所以概率和不为1了，这里要重新正则化一下。执行过程中Categorical会自己正则化。
        action_prob = action_prob / action_prob.sum(dim=-1, keepdim=True)
        # 因为有许多经验是填充的，它们的avail_actions都填充的是0，所以该经验上所有动作的概率都为0，在正则化的时候会得到nan。
        # 因此需要再一次将该经验对应的概率置为0
        action_prob[avail_actions == 0] = 0.0
        if self.args.cuda:
            action_prob = action_prob.cuda()

        return q_evals, q_targets, action_prob   #  torch.Size([1, 25, 5, 5])



    def get_q_values(self, batch, max_episode_len):
        episode_num = batch['o'].shape[0]
        q_evals, q_targets = [], []
        for transition_idx in range(max_episode_len):
            inputs, inputs_next = self._get_inputs(batch, transition_idx)  # 给obs加last_action、agent_id
            if self.args.cuda:
                inputs = inputs.cuda()
                inputs_next = inputs_next.cuda()
                self.eval_hidden = self.eval_hidden.cuda()
                self.target_hidden = self.target_hidden.cuda()
            q_eval, self.eval_hidden = self.eval_rnn(inputs, self.eval_hidden)  # 得到的q_eval维度为(episode_num*n_agents, n_actions)
            q_target, self.target_hidden = self.target_rnn(inputs_next, self.target_hidden)

            # 把q_eval维度重新变回(episode_num, n_agents, n_actions)
            q_eval = q_eval.view(episode_num, self.n_agents, -1)
            q_target = q_target.view(episode_num, self.n_agents, -1)
            q_evals.append(q_eval)
            q_targets.append(q_target)
        # 得的q_eval和q_target是一个列表，列表里装着max_episode_len个数组，数组的的维度是(episode个数, n_agents，n_actions)
        # 把该列表转化成(episode个数, max_episode_len， n_agents，n_actions)的数组
        q_evals = torch.stack(q_evals, dim=1)
        q_targets = torch.stack(q_targets, dim=1)
        return q_evals, q_targets   #  torch.Size([1, 25, 3, 5])

    def init_hidden(self, episode_num):
        # 为每个episode中的每个agent都初始化一个eval_hidden、target_hidden
        self.eval_hidden = torch.zeros((episode_num, self.n_agents, self.args.rnn_hidden_dim))
        self.target_hidden = torch.zeros((episode_num, self.n_agents, self.args.rnn_hidden_dim))

    def save_model(self, train_step):
        num = str(train_step // self.args.save_cycle)
        if not os.path.exists(self.model_dir):
            os.makedirs(self.model_dir)
        torch.save(self.eval_vdn_net.state_dict(), self.model_dir + '/' + num + '_vdn_net_params.pkl')
        torch.save(self.eval_rnn.state_dict(),  self.model_dir + '/' + num + '_rnn_net_params.pkl')
        torch.save(self.actor.state_dict(), self.model_dir + '/' + num + '_actor_params.pkl')
        torch.save(self.critic.state_dict(),  self.model_dir + '/' + num + '_critic_params.pkl')

    def train_dag_rl(self, batch, action_prob, q_evals_):  # torch.Size([1, 25, 5])
        data = batch
        episode_num = data['o'].shape[0]
        batch_size = data['o'].shape[1]
        shape_expand_action = self.args.n_actions
        shape_expand_agent = self.args.n_agents
        u, r = batch['u'], batch['r']
        u_onehot = torch.tensor(data['u_onehot'])
        agent_id_graph = torch.eye(self.args.n_agents).unsqueeze(0).unsqueeze(0).expand(episode_num, batch_size, -1, -1)  # 1.25.3.3
        u_onehot_last = torch.cat((torch.zeros_like(u_onehot[:, [0]]), u_onehot[:, 1:]), 1)
        inputs_graph = torch.cat((torch.tensor(data['o']), u_onehot_last, agent_id_graph), -1).float()
        inputs_graph = inputs_graph.reshape(episode_num*batch_size, inputs_graph.shape[-2], inputs_graph.shape[-1])

        encoder_output, samples, mask_scores, entropy, adj_prob, \
        log_probs_for_rewards, entropy_regularization = self.actor(inputs_graph)

        pre = self.critic.predict_rewards(encoder_output)  # 25
        # Reward config
        reward_mean = r[:, :, :, 0].mean()
        self.actor.avg_baseline = self.args.alpha * self.actor.avg_baseline + (1.0 - self.args.alpha) * reward_mean  # # moving baseline for Reinforce
        ################
        mask = 1 - batch["padded"].float()
        pi_taken = torch.gather(action_prob, dim=3, index=u).squeeze(3)  # torch.Size([1, 25, 5])
        pi_taken[mask.repeat(1, 1, pi_taken.shape[-1]) == 0] = 1.0
        log_pi_taken = torch.log(pi_taken)
        q_taken = torch.gather(q_evals_, dim=3, index=u).squeeze(3)  # torch.Size([1, 25, 5])
        baseline = (q_evals_ * action_prob).sum(dim=3, keepdim=True).squeeze(3).detach()  # torch.Size([1, 25, 5])

        pre_1 = pre.view(episode_num, -1).unsqueeze(-1).repeat(1,1,shape_expand_agent)
        # reward_baseline = r.squeeze(2) - baseline - pre_1
        reward_baseline = r.squeeze(2) - pre_1 + baseline

        advantage = (q_taken - reward_baseline).detach()
        log_adv = advantage * log_pi_taken

        # print(log_softmax_logits_for_rewards.shape) # 25.5.5
        log_probs_for_rewards_ = log_probs_for_rewards.view(episode_num, batch_size, shape_expand_agent, shape_expand_agent)
        mask = (1 - batch["padded"].float()).unsqueeze(-1).repeat(1, 1, shape_expand_agent, shape_expand_agent)


        loss_graph_actor = log_adv.unsqueeze(-1).repeat(1,1,1,shape_expand_agent) * log_probs_for_rewards_   ### TODO: graph generation prob
        loss_graph_actor = - ((loss_graph_actor) * mask).sum() / mask.sum()
        # mask_1 = (1 - batch["padded"].float()).unsqueeze(-1).unsqueeze(0)
        # loss_graph_actor += self.args.lambda_entropy * ((- entropy_regularization*mask_1).sum() / mask.sum())

        loss = 0
        # mask_scores_tensor = torch.stack(mask_scores).permute(1,0,2)
        for i in range(batch_size):
            m_s = adj_prob[i]
            # sparse_loss = self.args.tau_A * torch.sum(torch.abs(m_s))
            h_A = _h_A(m_s, self.args.n_agents)
            loss += h_A


        loss_hA = loss/batch_size
        loss_graph_actor = loss_graph_actor + loss_hA

        r = batch['r'][:, :, 0, 0].view(-1)
        mse_loss = torch.nn.MSELoss(reduction='mean')
        loss_graph_critic = 0.5 * mse_loss(pre, r)


        return loss_graph_actor, loss_graph_critic, loss_hA


    def train_dag_gnn_all(self, mini_batch, lambda_A, c_A):
        graph_loss = []
        # for _ in range(8):
            # data = self.buffer.sample(min(self.buffer.current_size, self.args.batch_size))  # 1.25.3.18
        data = mini_batch
        episode_num = data['o'].shape[1]
        batch_size = data['o'].shape[0]
        u_onehot = torch.tensor(data['u_onehot'])
        agent_id_graph = torch.eye(self.args.n_agents).unsqueeze(0).unsqueeze(0).expand(batch_size, episode_num, -1, -1) # 1.25.3.3
        u_onehot_last = torch.cat((torch.zeros_like(u_onehot[:, [0]]), u_onehot[:,1:]), 1)

        inputs_graph = torch.cat((torch.tensor(data['o']), u_onehot_last, agent_id_graph), -1)
        inputs_graph = Variable(inputs_graph).double()

        enc_x, logits, origin_A, adj_A_tilt_encoder, z_gap, z_positive, myA, Wa = self.encoder(inputs_graph)  # x, logits, adj_A1, adj_A, self.z, self.z_positive, self.adj_A, self.Wa
        edges = logits
        dec_x, output = self.decoder(edges, origin_A, Wa)

        if torch.sum(output != output):
            print('nan error\n')

        target = inputs_graph
        preds = output
        variance = 0.

        # reconstruction accuracy loss
        loss_nll = nll_gaussian(preds, target, variance)
        # KL loss
        loss_kl = kl_gaussian_sem(logits)
        # ELBO loss:
        loss = loss_kl + loss_nll
        # F.mse_loss(preds, target)
        # add A loss
        one_adj_A = origin_A  # torch.mean(adj_A_tilt_decoder, dim =0)
        sparse_loss = self.args.tau_A * torch.sum(torch.abs(one_adj_A))

        h_A = _h_A(origin_A, self.args.n_agents)
        loss += lambda_A * h_A + 0.5 * c_A * h_A * h_A + 100. * torch.trace(origin_A * origin_A) + sparse_loss

        myA.data = stau(myA.data, self.args.tau_A * self.args.lr_gnn)
        if torch.sum(origin_A != origin_A):
            print('nan error\n')
        # compute metrics
        graph = origin_A.data.clone().numpy()
        graph[np.abs(graph) < self.args.graph_threshold] = 0

            # graph_loss.append(loss)

        return loss


    def train_dag_gnn_outer(self, lambda_A, c_A):
        best_ELBO_loss = np.inf
        h_A_old = np.inf
        h_tol = 1e-8
        h_A_new = torch.tensor(1.)
        for step_k in range(self.args.k_max_iter):
            while c_A < 1e+20:  # 相当于notears里面的rho
                ELBO_loss, NLL_loss, MSE_loss, graph, origin_A = self.train_dag_innner(lambda_A, c_A,
                                                                                       self.dag_gnn_optimizer,
                                                                                       self.encoder,
                                                                                       self.decoder,
                                                                                       self.dag_gnn_scheduler)
                if ELBO_loss > 2 * best_ELBO_loss:
                    break
                # update parameters
                A_new = origin_A.data.clone()
                h_A_new = _h_A(A_new, self.args.n_agents)
                if h_A_new.item() > 0.25 * h_A_old:
                    c_A *= 10
                else:
                    break
            h_A_old = h_A_new.item()
            lambda_A += c_A * h_A_new.item()
            if h_A_new.item() <= h_tol:
                break
        return ELBO_loss, NLL_loss, MSE_loss, graph, origin_A


    def train_dag_innner(self, lambda_A, c_A, optimizer, encoder, decoder, scheduler):
        nll_train = []
        kl_train = []
        mse_train = []

        encoder.train()
        decoder.train()
        scheduler.step()

        # update optimizer
        optimizer, lr = update_optimizer(optimizer, self.args.lr_gnn, c_A)

        for _ in range(8):
            data = self.buffer.sample(min(self.buffer.current_size, self.args.batch_size))  # 1.25.3.18
            episode_num = data['o'].shape[1]
            batch_size = data['o'].shape[0]
            u_onehot = torch.tensor(data['u_onehot'])
            agent_id_graph = torch.eye(self.args.n_agents).unsqueeze(0).unsqueeze(0).expand(batch_size, episode_num, -1, -1) # 1.25.3.3
            u_onehot_last = torch.cat((torch.zeros_like(u_onehot[:, [0]]), u_onehot[:,1:]), 1)

            inputs_graph = torch.cat((torch.tensor(data['o']), u_onehot_last, agent_id_graph), -1)
            inputs_graph = Variable(inputs_graph).double()

            optimizer.zero_grad()
            enc_x, logits, origin_A, adj_A_tilt_encoder, z_gap, z_positive, myA, Wa = encoder(inputs_graph)  # x, logits, adj_A1, adj_A, self.z, self.z_positive, self.adj_A, self.Wa
            edges = logits
            dec_x, output = decoder(edges, origin_A, Wa)

            if torch.sum(output != output):
                print('nan error\n')

            target = inputs_graph
            preds = output
            variance = 0.

            # reconstruction accuracy loss
            loss_nll = nll_gaussian(preds, target, variance)

            # KL loss
            loss_kl = kl_gaussian_sem(logits)

            # ELBO loss:
            loss = loss_kl + loss_nll

            # add A loss
            one_adj_A = origin_A  # torch.mean(adj_A_tilt_decoder, dim =0)
            sparse_loss = self.args.tau_A * torch.sum(torch.abs(one_adj_A))

            h_A = _h_A(origin_A, self.args.n_agents)
            loss += lambda_A * h_A + 0.5 * c_A * h_A * h_A + 100. * torch.trace(origin_A * origin_A) + sparse_loss

            loss.backward()
            loss = optimizer.step()

            myA.data = stau(myA.data, self.args.tau_A * lr)
            if torch.sum(origin_A != origin_A):
                print('nan error\n')

            # compute metrics
            graph = origin_A.data.clone().numpy()
            graph[np.abs(graph) < self.args.graph_threshold] = 0

            mse_train.append(F.mse_loss(preds, target).item())
            nll_train.append(loss_nll.item())
            kl_train.append(loss_kl.item())

        return np.mean(np.mean(kl_train) + np.mean(nll_train)), np.mean(nll_train), np.mean(mse_train), graph, origin_A
